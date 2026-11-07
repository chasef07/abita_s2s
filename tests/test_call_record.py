"""The call record's rules: the chart computed from loads and settled writes, and the one write path."""

import asyncio
import unittest

from test_patient_resolution import receipt
from test_scheduling import appointment

from abita_s2s.call_record import (
    APPOINTMENT_WRITES,
    INSURANCE_WRITES,
    CallRecord,
    Registered,
    Send,
    WriteKey,
    WritePath,
    WritePlan,
    WriteSettled,
)
from abita_s2s.records import Appointment, Receipt

JANE = "chart-jane"


def chart(*ids, status="found", **extra) -> Receipt:
    return Receipt.model_validate(
        receipt(
            appointmentsStatus=status,
            appointments=[appointment(id=i) for i in ids],
            **extra,
        )
    )


def booked(appointment_id) -> Appointment:
    return Appointment.model_validate(appointment(id=appointment_id))


def settle(record, action, outcome="written", patient_id=JANE, target=None, **fx):
    key = WriteKey(patient_id, action, target)
    record.settled(WriteSettled(key, outcome, {"answer": f"{action} {outcome}"}, **fx))
    return key


def ids(record) -> list[int]:
    return [a.id for a in record.chart(JANE).appointments]


class ChartTests(unittest.TestCase):
    def test_a_load_wins_for_what_it_read_but_not_for_what_the_call_changed(self):
        record = CallRecord()
        record.loaded(chart(77))
        settle(record, "book", target="slot", booked=booked(888))
        settle(record, "cancel", target=77, cancelled_id=77)
        self.assertEqual(ids(record), [888])
        record.loaded(chart(77, 999, 888))
        self.assertEqual(ids(record), [999, 888])
        self.assertEqual(record.chart(JANE).appointmentsStatus, "found")

    def test_status_follows_what_the_effects_leave(self):
        record = CallRecord()
        loaded = chart(77)
        record.loaded(loaded)
        self.assertIs(record.chart(JANE), loaded)
        settle(record, "cancel", target=77, cancelled_id=77)
        self.assertEqual(record.chart(JANE).appointmentsStatus, "none")
        record.loaded(chart(status="none"))
        settle(record, "book", target="slot", booked=booked(888))
        self.assertEqual(record.chart(JANE).appointmentsStatus, "found")

    def test_a_rejected_write_revokes_the_loaded_appointments_until_a_newer_load(self):
        record = CallRecord()
        record.loaded(chart(77))
        settle(record, "book", "refused", target="slot")
        self.assertEqual(record.chart(JANE).appointmentsStatus, "found")
        settle(record, "cancel", "refused", target=77)
        self.assertEqual(record.chart(JANE).appointmentsStatus, "error")
        record.loaded(chart(77))
        self.assertEqual(record.chart(JANE).appointmentsStatus, "found")
        record.revoke(JANE)
        self.assertEqual(record.chart(JANE).appointmentsStatus, "error")

    def test_coverage_written_after_a_load_shows_until_the_next_load(self):
        record = CallRecord()
        record.loaded(chart(insuranceCarrierId="car-old", insuranceNote="Note"))
        settle(record, "update_insurance", target="aetna", coverage=("Aetna", "car1"))
        shown = record.chart(JANE)
        self.assertEqual(
            (shown.insuranceCarrier, shown.insuranceCarrierId, shown.insuranceNote),
            ("Aetna", "car1", None),
        )
        record.loaded(chart(insuranceCarrierId="car-old"))
        self.assertEqual(record.chart(JANE).insuranceCarrier, "Test Insurance")

    def test_a_read_older_than_a_settled_write_or_a_revocation_is_discarded(self):
        record = CallRecord()
        started = record.position()
        settle(record, "book", "refused", target="slot")
        settle(record, "cancel", "written", patient_id="chart-john", target=1)
        self.assertFalse(record.discards(started, JANE))
        settle(record, "update_insurance", "uncertain", target="aetna")
        self.assertTrue(record.discards(started, JANE))
        later = record.position()
        record.revoke(JANE)
        self.assertTrue(record.discards(later, JANE))
        self.assertFalse(record.discards(record.position(), JANE))


class WriteRuleTests(unittest.TestCase):
    def test_a_repeat_replays_the_last_settlement_until_one_that_does_not(self):
        record = CallRecord()
        key = settle(record, "book", target="slot")
        self.assertEqual(record.replayable(key).outcome, "written")
        settle(record, "book", "refused", target="slot")
        self.assertIsNone(record.replayable(key))

    def test_a_charts_blocker_answers_before_its_other_replays(self):
        record = CallRecord()
        booking = settle(record, "book", target="slot")
        cancel = settle(record, "cancel", "uncertain", target=77)
        self.assertIsNone(record.replayable(booking))
        self.assertEqual(record.replayable(cancel).outcome, "uncertain")
        self.assertEqual(record.blocker(JANE, APPOINTMENT_WRITES).key, cancel)
        self.assertIsNone(record.blocker("chart-john", APPOINTMENT_WRITES))
        self.assertIsNone(record.blocker(JANE, INSURANCE_WRITES))

    def test_insurance_replays_only_the_latest_and_a_registration_finishes_with_it(
        self,
    ):
        record = CallRecord()
        record.settled(
            WriteSettled(
                WriteKey(None, "register", "intake"),
                "written",
                {},
                registered=Registered(JANE, "partial"),
            )
        )
        self.assertEqual(record.registration(JANE), "partial")
        self.assertFalse(record.insurance_ready(JANE))
        first = settle(record, "update_insurance", target="a", coverage=("A", None))
        self.assertEqual(record.registration(JANE), "created")
        self.assertTrue(record.insurance_ready(JANE))
        settle(record, "update_insurance", target="b", coverage=("B", None))
        self.assertNotEqual(record.latest(JANE, INSURANCE_WRITES), first)
        self.assertEqual(record.coverage_writes(JANE), 2)


def plan(key, send=None, answer=None):
    return lambda: WritePlan(key, lambda: answer or send)


class WritePathTests(unittest.IsolatedAsyncioTestCase):
    def path(self, record, blocked=lambda key: None):
        return WritePath(
            record,
            APPOINTMENT_WRITES,
            closed={"answer": "closed"},
            busy={"answer": "busy"},
            blocked=blocked,
        )

    def send(self, key, gate=None, outcome="written", stale=None):
        async def request():
            if gate:
                await gate.wait()
            return WriteSettled(key, outcome, {"answer": "sent"})

        return Send(request, {"answer": "lost"}, lambda: stale)

    async def test_each_step_answers_in_order(self):
        record = CallRecord()
        key = WriteKey(JANE, "book", "slot")
        path = self.path(
            record, lambda k: {"answer": "blocked"} if k.target == "b" else None
        )
        gate = asyncio.Event()
        first = asyncio.create_task(path.run(plan(key, self.send(key, gate))))
        await asyncio.sleep(0)
        self.assertTrue(record.in_flight(APPOINTMENT_WRITES))
        self.assertEqual(await path.run(plan(key, self.send(key))), {"answer": "busy"})
        gate.set()
        self.assertEqual(await first, {"answer": "sent"})
        self.assertEqual(await path.run(plan(key, self.send(key))), {"answer": "sent"})
        blocked = WriteKey(JANE, "book", "b")
        self.assertEqual(
            await path.run(plan(blocked, self.send(blocked))), {"answer": "blocked"}
        )
        fresh = WriteKey(JANE, "book", "c")
        self.assertEqual(
            await path.run(plan(fresh, answer={"answer": "confirm first"})),
            {"answer": "confirm first"},
        )
        self.assertIsNone(record.replayable(fresh))
        record.close()
        self.assertEqual(
            await path.run(plan(fresh, self.send(fresh))), {"answer": "closed"}
        )

    async def test_a_change_as_the_send_starts_settles_unsent(self):
        record = CallRecord()
        key = WriteKey(JANE, "cancel", 77)
        path = self.path(record)
        result = await path.run(plan(key, self.send(key, stale={"answer": "stale"})))
        self.assertEqual(result, {"answer": "stale"})
        self.assertFalse(record.in_flight(APPOINTMENT_WRITES))
        self.assertIsNone(record.replayable(key))

    async def test_an_exception_settles_uncertain_and_fails_the_drain(self):
        record = CallRecord()
        key = WriteKey(JANE, "cancel", 77)
        path = self.path(record)

        async def request():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await path.run(plan(key, Send(request, {"answer": "lost"}, lambda: None)))
        self.assertEqual(record.replayable(key).answer, {"answer": "lost"})
        self.assertEqual(record.blocker(JANE, APPOINTMENT_WRITES).key, key)
        with self.assertRaises(RuntimeError):
            await path.drain()
