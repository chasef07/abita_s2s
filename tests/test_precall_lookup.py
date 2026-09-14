import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from abita_s2s.identity import (
    activate_verified_patient,
    begin_lookup,
    clear_active_patient,
)
from abita_s2s.middleware import (
    HydratedPatient,
    MiddlewareFailure,
    PatientMatches,
    PatientNotFound,
)
from abita_s2s.runtime.precall_lookup import precall_lookup
from abita_s2s.state import CallContext, CallState, PatientCandidate, VerifiedPatient


def state(phone="+15555550101"):
    return CallState(
        CallContext(
            "call", datetime.now(timezone.utc), "test", "office", caller_phone=phone
        )
    )


class PrecallLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_hydrated_evidence_retained_without_patient_activation(self):
        record = HydratedPatient.model_validate_json("""{
          "status":"verified", "patientId":"chart", "name":"Test Person", "dob":"01/01/1990",
          "insuranceCarrier":"Test plan", "appointmentsStatus":"found", "appointments":[
            {"id":1,"date":"2026-09-30","time":"09:00","cancellationToken":"private-token"}
          ]}""")
        call = state()
        client = AsyncMock()
        client.resolve_patient.return_value = record
        await precall_lookup(call, client, office="canonical-office")
        client.resolve_patient.assert_awaited_once_with(
            office="canonical-office", phone="+15555550101"
        )
        self.assertIs(call.patient.lookup.candidates[0], record)
        self.assertEqual(record.appointments[0].cancellation_token, "private-token")
        self.assertIsNone(call.patient.active)

    async def test_multiple_none_and_failure_remain_distinct(self):
        candidate = PatientCandidate("chart", "Test", "Person", "01/01/1990")
        for result, expected in (
            (PatientMatches((candidate,)), "found"),
            (PatientNotFound(), "none"),
            (MiddlewareFailure("network_error"), "failed"),
        ):
            call = state()
            client = AsyncMock()
            client.resolve_patient.return_value = result
            await precall_lookup(call, client, office="office")
            self.assertEqual(call.patient.lookup.status, expected)
            self.assertIsNone(call.patient.active)

    async def test_missing_phone_does_not_call_backend(self):
        call, client = state(None), AsyncMock()
        await precall_lookup(call, client, office="office")
        client.resolve_patient.assert_not_awaited()
        self.assertEqual(call.patient.lookup.status, "not_attempted")

    async def test_late_result_cannot_overwrite_newer_identity_work(self):
        call = state()
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolve(**kwargs):
            entered.set()
            await release.wait()
            return PatientNotFound()

        client = AsyncMock()
        client.resolve_patient.side_effect = resolve
        task = asyncio.create_task(precall_lookup(call, client, office="office"))
        await entered.wait()
        clear_active_patient(call.patient)
        patient = VerifiedPatient("new-chart", "Other Person", "1980-01-01")
        activate_verified_patient(call.patient, begin_lookup(call.patient), patient)
        release.set()
        await task
        self.assertIs(call.patient.active, patient)
        self.assertEqual(call.patient.lookup.status, "not_attempted")

    async def test_cancelled_read_does_not_become_no_match(self):
        call, client = state(), AsyncMock()
        client.resolve_patient.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await precall_lookup(call, client, office="office")
        self.assertEqual(call.patient.lookup.status, "not_attempted")
