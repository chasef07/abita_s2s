import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, Mock

from test_patient_resolution import call_state, receipt

from abita_s2s.identity import PatientResolver
from abita_s2s.integrations.patient_middleware import NotFound, Receipt
from abita_s2s.state import CallState, CandidateLookup


class CallStateTests(unittest.IsolatedAsyncioTestCase):
    def resolver(self):
        resolver = PatientResolver(call_state(), Mock())
        self.addAsyncCleanup(resolver.aclose)
        return resolver

    async def test_calls_do_not_share_patient_state_or_expose_records_in_repr(self):
        resolver = self.resolver()
        other = CallState(resolver.state.call)
        patient = Receipt.model_validate(receipt())
        await resolver._load_patient(
            patient, "Jane", None, resolver._begin_lookup(), phone=True
        )
        self.assertIs(resolver.state.patient.active, patient)
        self.assertIsNone(other.patient.active)
        self.assertNotIn("Jane", repr(resolver.state))
        self.assertNotIn("Jane", repr(patient))
        self.assertNotIn("01/02/1980", repr(patient))
        with self.assertRaises(FrozenInstanceError):
            resolver.state.call.called_office_key = "different"

    async def test_stale_cross_call_and_replayed_receipts_are_rejected(self):
        resolver, other = self.resolver(), self.resolver()
        old = resolver._begin_lookup()
        current = resolver._begin_lookup()
        patient = Receipt.model_validate(receipt())
        self.assertEqual(
            (await resolver._load_patient(patient, "Jane", None, old, phone=True))[
                "outcome"
            ],
            "superseded",
        )
        self.assertEqual(
            (await other._load_patient(patient, "Jane", None, current, phone=True))[
                "outcome"
            ],
            "superseded",
        )
        self.assertIsNone(resolver.state.patient.active)
        self.assertIsNone(other.state.patient.active)
        self.assertEqual(
            (await resolver._load_patient(patient, "Jane", None, current, phone=True))[
                "outcome"
            ],
            "verified",
        )
        revision = resolver.state.patient.revision
        for token in (current, None):
            self.assertEqual(
                (
                    await resolver._load_patient(
                        patient, "Jane", None, token, phone=True
                    )
                )["outcome"],
                "superseded",
            )
        self.assertEqual(resolver.state.patient.revision, revision)
        resolver._middleware.resolve.assert_not_called()

    async def test_private_lookup_cannot_activate_or_be_replayed(self):
        resolver, other = self.resolver(), self.resolver()
        patient = Receipt.model_validate(receipt())
        token = resolver._begin_lookup()
        other._middleware.resolve = AsyncMock(return_value=patient)
        resolver._middleware.resolve = AsyncMock(
            side_effect=[
                patient,
                NotFound(status="not_found"),
                NotFound(status="not_found"),
            ]
        )
        await other._lookup_phone(token)
        self.assertEqual(other.state.patient.lookup.status, "not_attempted")
        await resolver._lookup_phone(token)
        self.assertIsNone(resolver.state.patient.active)
        await resolver._lookup_phone(token)
        self.assertEqual(resolver.state.patient.lookup.status, "found")
        # A fresh private read also cannot replace the active patient.
        await resolver._load_patient(
            patient, "Jane", None, resolver._begin_lookup(), phone=True
        )
        await resolver._lookup_phone(resolver._begin_lookup())
        self.assertIs(resolver.state.patient.active, patient)

    def test_lookup_failure_is_distinct_from_absence(self):
        self.assertEqual(
            CandidateLookup("failed", failure_reason="network_error").status, "failed"
        )
        self.assertIsNone(CandidateLookup("none").failure_reason)
        for status in ("unexpected", "failed", "found"):
            with self.assertRaises(ValueError):
                CandidateLookup(status)
