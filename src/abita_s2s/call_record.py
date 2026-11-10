"""One call's record of chart loads and write results, in order, and the one path every write takes.

The chart is computed from the record: the last load plus the effects writes confirmed.

Outcomes of a sent write:
    written      the change is confirmed.
    partial      part of the change is confirmed; staff must finish it.
    uncertain    the change may or may not have happened.
    refused      the middleware refused the request; the write did not take effect.
    expired      the middleware refused the target's token; the write did not take effect.
    failed       the middleware could not write; the write did not take effect.
    unsent       the request never reached the record.
    needs_input  the middleware needs more facts; nothing changed.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Hashable, Iterator
from dataclasses import dataclass
from typing import Literal

from abita_s2s.records import Appointment, Receipt

WriteOutcome = Literal[
    "written",
    "partial",
    "uncertain",
    "refused",
    "expired",
    "failed",
    "unsent",
    "needs_input",
]
WriteAction = Literal["book", "cancel", "reschedule", "register", "update_insurance"]
APPOINTMENT_WRITES: Collection[WriteAction] = ("book", "cancel", "reschedule")
INSURANCE_WRITES: Collection[WriteAction] = ("register", "update_insurance")
REPLAYS: Collection[WriteOutcome] = ("written", "partial", "uncertain")
BLOCKS: Collection[WriteOutcome] = ("partial", "uncertain")
SPENDS_TARGET: Collection[WriteOutcome] = ("refused", "expired", "failed")


@dataclass(frozen=True)
class WriteKey:
    """Whose chart a write changes, the action, and what it acts on. A new chart has no patient yet."""

    patient_id: str | None
    action: WriteAction
    target: Hashable = None


@dataclass(frozen=True, repr=False)
class Registered:
    """The chart a registration created, and whether its insurance attached."""

    patient_id: str
    status: Literal["created", "partial"]


@dataclass(frozen=True, repr=False)
class ChartLoaded:
    """A chart read from the practice system."""

    chart: Receipt


@dataclass(frozen=True, repr=False)
class WriteSent:
    key: WriteKey


@dataclass(frozen=True, repr=False)
class WriteSettled:
    """How a write ended: the answer it gave, and the effects it confirmed on the chart."""

    key: WriteKey
    outcome: WriteOutcome
    answer: dict
    booked: Appointment | None = None
    cancelled_id: int | None = None
    selection: Hashable = None
    coverage: tuple[str, str | None] | None = None
    registered: Registered | None = None

    @property
    def revokes(self) -> bool:
        """A rejected cancel or move means the loaded tokens must be reloaded first."""
        return self.key.action in ("cancel", "reschedule") and (
            self.outcome in SPENDS_TARGET
        )

    @property
    def outdates_reads(self) -> bool:
        """A read of this chart that started before now may predate what this write did."""
        return self.revokes or self.outcome in REPLAYS


Entry = ChartLoaded | WriteSent | WriteSettled


class CallRecord:
    """What happened on this call. Everything else about a write is computed from it."""

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self.closed = False

    def close(self) -> None:
        """Admit nothing more. Writes already sent still settle."""
        self.closed = True

    def position(self) -> int:
        """Where the record ends now. A read remembers it to learn what happened since."""
        return len(self._entries)

    def loaded(self, chart: Receipt) -> int:
        """Record a chart read; return its position."""
        self._entries.append(ChartLoaded(chart))
        return len(self._entries) - 1

    def sent(self, key: WriteKey) -> None:
        self._entries.append(WriteSent(key))

    def settled(self, settled: WriteSettled) -> None:
        self._entries.append(settled)

    def revoke(self, patient_id: str) -> None:
        """The chart's loaded tokens stop counting until a newer load, as a refused move would."""
        self._entries.append(
            WriteSettled(WriteKey(patient_id, "reschedule"), "expired", {})
        )

    def discards(self, position: int, patient_id: str) -> bool:
        """Whether a read of this chart that started at position predates a settled write or a revocation."""
        return any(
            isinstance(entry, WriteSettled)
            and entry.key.patient_id == patient_id
            and entry.outdates_reads
            for entry in self._entries[position:]
        )

    def chart(self, patient_id: str) -> Receipt | None:
        """The chart's last load plus every effect this call's writes confirmed on it.

        A load wins for the appointments and coverage it read. An appointment this call
        cancelled stays cancelled and one it booked stays listed, whatever a load says.
        A rejected write revokes the loaded appointments until a newer load.
        """
        load: Receipt | None = None
        coverage: tuple[str, str | None] | None = None
        revoked = False
        booked: dict[int, Appointment] = {}
        cancelled: set[int] = set()
        for entry in self._entries:
            if isinstance(entry, ChartLoaded) and entry.chart.patientId == patient_id:
                load, coverage, revoked = entry.chart, None, False
            elif isinstance(entry, WriteSettled) and entry.key.patient_id == patient_id:
                if entry.booked:
                    booked.setdefault(entry.booked.id, entry.booked)
                if entry.cancelled_id is not None:
                    cancelled.add(entry.cancelled_id)
                coverage = entry.coverage or coverage
                revoked = revoked or entry.revokes
        if load is None:
            return None
        appointments = [a for a in load.appointments if a.id not in cancelled]
        listed = {a.id for a in appointments}
        appointments += [
            a for i, a in booked.items() if i not in cancelled and i not in listed
        ]
        status = load.appointmentsStatus
        if appointments != load.appointments and status != "error":
            status = "found" if appointments else "none"
        changes: dict[str, object] = {}
        if appointments != load.appointments or revoked:
            changes.update(
                appointments=appointments,
                appointmentsStatus="error" if revoked else status,
            )
        if coverage:
            changes.update(
                insuranceCarrier=coverage[0],
                insuranceCarrierId=coverage[1],
                insuranceNote=None,
            )
        return load.model_copy(update=changes) if changes else load

    def _settled(self) -> Iterator[WriteSettled]:
        return (e for e in self._entries if isinstance(e, WriteSettled))

    def _kept(self) -> dict[WriteKey, WriteSettled]:
        """Each key's last settlement when it replays. A later one that does not forgets it."""
        kept: dict[WriteKey, WriteSettled] = {}
        for settled in self._settled():
            kept.pop(settled.key, None)
            if settled.outcome in REPLAYS:
                kept[settled.key] = settled
        return kept

    def in_flight(self, actions: Collection[WriteAction]) -> bool:
        sent: set[WriteKey] = set()
        for entry in self._entries:
            if isinstance(entry, WriteSent) and entry.key.action in actions:
                sent.add(entry.key)
            elif isinstance(entry, WriteSettled):
                sent.discard(entry.key)
        return bool(sent)

    def blockers(
        self, actions: Collection[WriteAction], patient_id: str | None = None
    ) -> list[WriteSettled]:
        """Half-done or uncertain writes staff must finish first, on one chart if named."""
        return [
            s
            for s in self._kept().values()
            if s.key.action in actions
            and s.outcome in BLOCKS
            and patient_id in (None, s.key.patient_id)
        ]

    def saved(self, key: WriteKey) -> WriteSettled | None:
        """The settlement a repeat of this write would answer from."""
        return self._kept().get(key)

    def latest(
        self, patient_id: str, actions: Collection[WriteAction]
    ) -> WriteKey | None:
        """The last kept write among these actions on this chart."""
        keys = [
            key
            for key in self._kept()
            if key.patient_id == patient_id and key.action in actions
        ]
        return keys[-1] if keys else None

    def cancelled(self, patient_id: str, appointment_id: int) -> bool:
        return any(
            s.key.patient_id == patient_id and s.cancelled_id == appointment_id
            for s in self._settled()
        )

    def coverage_writes(self, patient_id: str) -> int:
        """How many insurance updates this call confirmed on this chart."""
        return sum(
            s.key.patient_id == patient_id and s.coverage is not None
            for s in self._settled()
        )

    def registration(self, patient_id: str) -> Literal["created", "partial"] | None:
        """How this call registered the chart. A later insurance update finishes a partial one."""
        status = None
        for settled in self._settled():
            if settled.registered and settled.registered.patient_id == patient_id:
                status = settled.registered.status
            elif status and settled.coverage and settled.key.patient_id == patient_id:
                status = "created"
        return status

    def insurance_ready(self, patient_id: str | None) -> bool:
        """No insurance write is open or unresolved, and the chart's registration finished."""
        return not (
            self.in_flight(INSURANCE_WRITES)
            or self.blockers(INSURANCE_WRITES)
            or (patient_id and self.registration(patient_id) == "partial")
        )


Request = Callable[[], Awaitable[WriteSettled]]


@dataclass(frozen=True, repr=False)
class WritePlan:
    """One write. prepare checks it and returns its request, or the answer refusing it.

    unknown is the answer kept if the result is lost. stale says why the request must not
    leave after all, for a change that landed as its task started. replay decides what a
    repeat answers from the saved settlement; None sends again.
    """

    key: WriteKey
    prepare: Callable[[], Request | dict]
    unknown: dict
    stale: Callable[[], dict | None]
    replay: Callable[[WriteSettled], dict | None] | None = None


class WritePath:
    """Every write takes these steps in this order:

    closed, in-flight, replay, block, prepare, WriteSent, shielded send, settle.

    Each owner has its own path, so a write in flight or a blocker stops only that
    owner's writes: its chart's, or with blocks_call, the whole call's.
    Admission finishes before the first await, so nothing else can run between the checks
    and WriteSent. An exception settles the write as uncertain and fails the drain.
    """

    def __init__(
        self,
        record: CallRecord,
        actions: Collection[WriteAction],
        *,
        closed: dict,
        busy: dict,
        blocks_call: dict | None = None,
    ):
        self._record = record
        self._actions = actions
        self._closed = closed
        self._busy = busy
        self._blocks_call = blocks_call
        self._last_sent: asyncio.Task[dict] | None = None

    async def run(self, plan: Callable[[], WritePlan | dict]) -> dict:
        if self._record.closed:
            return self._closed
        if self._record.in_flight(self._actions):
            return self._busy
        write = plan()
        if isinstance(write, dict):
            return write
        key = write.key
        own = (
            self._record.blockers(self._actions, key.patient_id)
            if key.patient_id
            else []
        )
        if (not own or own[0].key == key) and (kept := self._record.saved(key)):
            answer = write.replay(kept) if write.replay else kept.answer
            if answer is not None:
                return answer
        if own:
            return own[0].answer
        if self._blocks_call and self._record.blockers(self._actions):
            return self._blocks_call
        request = write.prepare()
        if isinstance(request, dict):
            return request
        self._record.sent(write.key)
        self._last_sent = asyncio.create_task(self._send(write, request))
        return await asyncio.shield(self._last_sent)

    async def _send(self, write: WritePlan, request: Request) -> dict:
        try:
            if (answer := write.stale()) is not None:
                settled = WriteSettled(write.key, "unsent", answer)
            else:
                settled = await request()
        except BaseException:
            self._record.settled(WriteSettled(write.key, "uncertain", write.unknown))
            raise
        self._record.settled(settled)
        return settled.answer

    async def drain(self) -> None:
        """Wait for the last write sent. Its exception reaches shutdown, which reports the failed drain."""
        if self._last_sent:
            await asyncio.shield(self._last_sent)
