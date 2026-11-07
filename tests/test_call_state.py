import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from test_patient_resolution import call_state, candidate, receipt

from abita_s2s.identity import PatientResolver
from abita_s2s.integrations.patient_middleware import NotFound
from abita_s2s.offices import get_office_profile
from abita_s2s.records import Candidate, Receipt
from abita_s2s.state import CallContext, CallState


class CallStateTests(unittest.IsolatedAsyncioTestCase):
    def test_office_phone_keeps_the_dialed_spelling_and_names_its_product_location(
        self,
    ):
        for called, phone, key in (
            (None, "+17864657475", "sweetwater"),
            ("", "+17864657475", "sweetwater"),
            ("+17864654845", "+17864654845", "sweetwater"),
            ("1-786-465-7479", "1-786-465-7479", "sweetwater-optical"),
        ):
            with self.subTest(called=called):
                call = CallContext(
                    "call",
                    datetime.now(UTC),
                    get_office_profile("sweetwater"),
                    called_number=called,
                )
                self.assertEqual(call.office_phone, phone)
                self.assertEqual(call.product_office_key, key)

    def resolver(self):
        resolver = PatientResolver(call_state(), Mock())
        self.addAsyncCleanup(resolver.aclose)
        return resolver

    async def test_calls_do_not_share_patient_state_or_expose_records_in_repr(self):
        resolver = self.resolver()
        other = CallState(resolver.state.call)
        patient = Receipt.model_validate(receipt())
        await resolver._load_patient(
            patient, "Jane", None, resolver._begin_lookup(), phone=False
        )
        self.assertIs(resolver.state.chart(), patient)
        self.assertIsNone(other.chart())
        self.assertNotIn("Jane", repr(resolver.state))
        self.assertNotIn("Jane", repr(patient))
        self.assertNotIn("01/02/1980", repr(patient))
        with self.assertRaises(FrozenInstanceError):
            resolver.state.call.office = get_office_profile("hollywood")

    async def test_stale_cross_call_and_replayed_receipts_are_rejected(self):
        resolver, other = self.resolver(), self.resolver()
        old = resolver._begin_lookup()
        current = resolver._begin_lookup()
        patient = Receipt.model_validate(receipt())
        self.assertEqual(
            (await resolver._load_patient(patient, "Jane", None, old, phone=False))[
                "outcome"
            ],
            "superseded",
        )
        self.assertEqual(
            (await other._load_patient(patient, "Jane", None, current, phone=False))[
                "outcome"
            ],
            "superseded",
        )
        self.assertIsNone(resolver.state.chart())
        self.assertIsNone(other.state.chart())
        self.assertEqual(
            (await resolver._load_patient(patient, "Jane", None, current, phone=False))[
                "outcome"
            ],
            "verified",
        )
        since = resolver.state.identity.since
        for token in (current, None):
            self.assertEqual(
                (
                    await resolver._load_patient(
                        patient, "Jane", None, token, phone=False
                    )
                )["outcome"],
                "superseded",
            )
        self.assertEqual(resolver.state.identity.since, since)
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
        self.assertEqual(other._candidates, ())
        await resolver._lookup_phone(token)
        self.assertIsNone(resolver.state.chart())
        await resolver._lookup_phone(token)
        self.assertEqual(
            resolver._candidates,
            (Candidate.model_validate(candidate()),),
        )
        await resolver._load_patient(
            patient, "Jane", None, resolver._begin_lookup(), phone=False
        )
        await resolver._lookup_phone(resolver._begin_lookup())
        self.assertIs(resolver.state.chart(), patient)
