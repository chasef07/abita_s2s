"""Availability: slot identity, readable references, office choice and the offered inventory."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from abita_s2s.insurance import decision_answer
from abita_s2s.insurance_contract import COVERAGE_TYPES, CoverageType
from abita_s2s.insurance_list import listed_plan
from abita_s2s.insurance_state import insurance_ready
from abita_s2s.integrations.scheduling_http import (
    SchedulingFailure,
    SchedulingHTTP,
    Slot,
    SlotSearch,
)
from abita_s2s.offices import EASTERN, get_office_profile
from abita_s2s.results import reply
from abita_s2s.state import CallState

SHARED_OFFICE_CHOICE = "needs_input: Ask for Hollywood or Sweetwater on those office calls; omit office for other calls."


def provider_name(name):
    for old, new in (
        ("Dr. Austin Bach (Overflow)", "Dr. Bach"),
        ("Dr. Austin Bach", "Dr. Bach"),
        ("Dr. J. Licht", "Dr. Licht"),
        ("Dr. D. Noel", "Dr. Noel"),
    ):
        name = name.replace(old, new)
    return name


def eastern_datetime(value: str) -> datetime:
    start = datetime.fromisoformat(value)
    return (
        start.replace(tzinfo=EASTERN)
        if start.tzinfo is None
        else start.astimezone(EASTERN)
    )


def slot_identity(slot: Slot) -> tuple[str, datetime]:
    """One bookable choice per provider and start, whichever middleware column backs it."""
    return provider_name(slot.provider), eastern_datetime(slot.datetime)


def afternoon(start: datetime) -> bool:
    return start.hour >= 12


def slot_reference(slot: Slot) -> str:
    """A readable reference such as SEP15-0900-BACH: date, 24-hour time, provider surname."""
    surname = "".join(filter(str.isalpha, provider_name(slot.provider).split()[-1]))
    return f"{eastern_datetime(slot.datetime):%b%d-%H%M}-{surname}".upper()


def soonest(offered: list[tuple[str, Slot]]) -> list[str]:
    """Refs to offer first: open slots on the earliest day, one morning and one afternoon.

    When that day has only shared starts, offer its earliest shared start and the
    earliest open slot on a later day.
    """
    starts = [(ref, slot, eastern_datetime(slot.datetime)) for ref, slot in offered]
    first_day = starts[0][2].date()
    day = [item for item in starts if item[2].date() == first_day]
    open_slots = [(ref, start) for ref, slot, start in day if not slot.sameStartBooked]
    if not open_slots:
        later = [
            ref
            for ref, slot, start in starts
            if start.date() != first_day and not slot.sameStartBooked
        ]
        return [day[0][0], *later[:1]]
    halves = [
        [ref for ref, start in open_slots if afternoon(start) is half]
        for half in (False, True)
    ]
    if all(halves):
        return [half[0] for half in halves]
    return [ref for ref, _ in open_slots[:2]]


@dataclass(frozen=True, repr=False)
class SchedulingContext:
    patient_revision: int
    patient_id: str | None
    dob: str | None
    insurance_write_pending: bool
    insurance_write_uncertain: bool
    registration: str | None


def scheduling_context(state: CallState) -> SchedulingContext:
    """The patient and insurance facts an offer or a write was made under."""
    p = state.patient.active
    insurance = state.insurance
    registration = insurance.registrations.get(p.patientId) if p else None
    return SchedulingContext(
        patient_revision=state.patient.revision,
        patient_id=p.patientId if p else None,
        dob=p.dob if p else None,
        insurance_write_pending=insurance.write_pending,
        insurance_write_uncertain=insurance.write_uncertain,
        registration=registration,
    )


@dataclass(frozen=True, repr=False)
class AvailabilitySearch:
    context: SchedulingContext
    office: str
    visit: CoverageType
    plan_id: str
    start: date
    today: date


@dataclass(frozen=True, repr=False)
class OfferedSlot:
    slot: Slot
    context: SchedulingContext
    office: str
    visit: CoverageType
    expires: datetime

    @property
    def selection(self):
        return self.office, self.visit, slot_identity(self.slot)


@dataclass(repr=False)
class PendingRead:
    task: asyncio.Task[dict]
    waiters: int = 0


class Offers:
    """The slots offered to the caller, the search cache and the bounded retries behind them."""

    def __init__(
        self, state: CallState, http: SchedulingHTTP, now: Callable[[], datetime]
    ):
        self.state = state
        self.http = http
        self._now = now
        self._slots: dict[str, OfferedSlot] = {}
        self._issued: dict[str, int] = {}
        self._generation = 0
        self._search_key: AvailabilitySearch | None = None
        self._search: PendingRead | None = None
        self._read_tasks: set[asyncio.Task[dict]] = set()
        self._cache: tuple[datetime, dict] | None = None
        self._failures: dict[AvailabilitySearch, tuple[int, bool]] = {}
        self._announced_promoted_office = False

    def get(self, ref: str) -> OfferedSlot | None:
        return self._slots.get(ref)

    def issued(self, ref: str) -> int | None:
        """The search generation that issued a reference, which keys a booking."""
        return self._issued.get(ref)

    def invalidate(self) -> None:
        self._generation += 1
        self._slots.clear()
        self._cache = None
        self._search_key = None
        self._search = None

    async def aclose(self) -> None:
        for task in self._read_tasks:
            task.cancel()
        await asyncio.gather(*self._read_tasks, return_exceptions=True)

    def _office(self, requested, visit) -> tuple[str, None] | tuple[None, str]:
        """The office to search, or the question to put to the caller first."""
        office = get_office_profile(self.state.call.called_office_key)
        promotion = office.promotion
        if promotion and visit == promotion.visit:
            if not self._announced_promoted_office and requested in (None, office.key):
                self._announced_promoted_office = True
                return None, promotion.announcement
            if requested in (*office.scheduling_offices, promotion.office):
                return requested, None
            return None, promotion.follow_up
        elif office.scheduling_offices:
            if requested in office.scheduling_offices:
                return requested, None
        elif requested is None:
            return office.key, None
        return None, SHARED_OFFICE_CHOICE

    async def search(
        self, visit, plan_id: str, start=None, office=None, *, writing: bool
    ) -> dict:
        context = scheduling_context(self.state)
        today = self._now().astimezone(EASTERN).date()
        try:
            first = date.fromisoformat(start) if start else today + timedelta(days=1)
            if start and first.isoformat() != start:
                raise ValueError()
        except ValueError:
            self.invalidate()
            return reply(
                "needs_input", "needs_input: Provide the requested date as YYYY-MM-DD."
            )
        if first <= today:
            self.invalidate()
            return reply(
                "needs_input",
                "needs_input: Same-day and past dates cannot be scheduled here. Ask whether tomorrow or later works; do not silently change the date.",
            )
        choice = self._office(office, visit)
        if choice[1] is not None:
            self.invalidate()
            return reply("needs_input", choice[1])
        selected = choice[0]
        p = self.state.patient.active
        if visit not in COVERAGE_TYPES or p is None or not insurance_ready(self.state):
            self.invalidate()
            return reply(
                "needs_input",
                "needs_input: Verify the patient, finish new-patient registration, or resolve the pending or uncertain insurance update before scheduling.",
            )
        plan = listed_plan(self.state, plan_id, visit)
        if isinstance(plan, dict):
            self.invalidate()
            return plan
        if writing:
            return reply(
                "in_progress",
                "blocked: An appointment change is in progress. Wait for its result.",
            )
        key = AvailabilitySearch(context, selected, visit, plan.planId, first, today)
        if self._search_key != key:
            self.invalidate()
            self._search_key = key
        if self._cache and self._cache[0] > self._now():
            return self._cache[1]
        failed = self._failures.get(key)
        if failed and (failed[0] >= 2 or not failed[1]):
            return reply(
                "availability_failed",
                "blocked: Availability could not be verified. Do not describe this as no openings or retry this search; ask staff for help.",
            )
        if self._search and not self._search.task.done():
            pending = self._search
        else:
            search = SlotSearch(
                office=selected,
                startDate=first.isoformat(),
                patientId=p.patientId,
                visitType=visit,
                insurancePlanId=plan.planId,
                dob=p.dob,
            )
            task = asyncio.create_task(self._load(search, key, self._generation))
            pending = self._search = PendingRead(task)
            self._read_tasks.add(task)
            task.add_done_callback(self._read_tasks.discard)
        pending.waiters += 1
        try:
            return await asyncio.shield(pending.task)
        finally:
            pending.waiters -= 1
            if pending.waiters == 0 and not pending.task.done():
                pending.task.cancel()
                if self._search is pending:
                    self.invalidate()

    async def _load(
        self, search: SlotSearch, key: AvailabilitySearch, generation: int
    ) -> dict:
        result = await self.http.availability(search)
        if generation != self._generation or key.context != scheduling_context(
            self.state
        ):
            return reply(
                "stale",
                "blocked: The patient or appointment details changed. Check again with current details.",
            )
        self._slots.clear()
        first = key.start.isoformat()
        through = (key.start + timedelta(days=13)).isoformat()
        expired = False
        if not isinstance(result, SchedulingFailure):
            decision = result.insuranceDecision
            if (
                (result.searchedFrom is not None and result.searchedFrom != first)
                or (
                    result.searchedThrough is not None
                    and result.searchedThrough != through
                )
                or any(not first <= slot.date <= through for slot in result.slots)
            ):
                result = SchedulingFailure(reason="invalid_search_window")
            elif decision is not None and (
                decision.coverageType != key.visit
                or decision.planId not in ("", key.plan_id)
            ):
                result = SchedulingFailure(reason="mismatched_insurance_decision")
            elif decision is not None and result.outcome == "insurance_not_cleared":
                self._failures.pop(key, None)
                self.invalidate()
                return reply(
                    "insurance_not_cleared",
                    f"blocked: {decision_answer(decision)} Offer a staff request or transfer; do not schedule.",
                )
            elif result.outcome == "availability_found":
                expiry = result.bookingTokenExpiresAt
                if expiry is not None and expiry > self._now():
                    self._failures.pop(key, None)
                    return self._offer(result.slots, key, generation, expiry)
                expired = True
                result = SchedulingFailure(reason="expired_inventory")
        if (
            not isinstance(result, SchedulingFailure)
            and result.outcome == "invalid_input"
        ):
            answer = reply(
                "needs_input",
                f"needs_input: {result.message}",
                retry_same_search=False,
            )
            self._failures.pop(key, None)
            return self._remember(answer)
        if (
            isinstance(result, SchedulingFailure)
            or result.outcome == "availability_search_incomplete"
            or result.status == "error"
        ):
            retry = (
                isinstance(result, SchedulingFailure) or result.shouldRetrySameSearch
            )
            count = self._failures.get(key, (0, False))[0] + 1
            self._failures[key] = (count, retry)
            if expired and count < 2:
                return await self._load(search, key, generation)
            return reply(
                "availability_failed",
                "blocked: Availability could not be verified; this does not mean no openings.",
                retry_same_search=retry and count < 2,
            )
        self._failures.pop(key, None)
        if result.outcome == "no_eligible_providers":
            answer = reply(
                "unsupported",
                "blocked: No providers are eligible for the selected office, visit type, and patient requirements. Confirm the office and visit type or ask staff for help; changing dates will not resolve this restriction.",
            )
            return self._remember(answer)
        answer = reply(
            "none",
            "no_results: No eligible openings in the searched window. Ask what other dates work.",
            searchedFrom=first,
            searchedThrough=through,
        )
        return self._remember(answer)

    def _offer(
        self,
        slots: list[Slot],
        key: AvailabilitySearch,
        generation: int,
        expiry: datetime,
    ) -> dict:
        first = key.start.isoformat()
        through = (key.start + timedelta(days=13)).isoformat()
        choices = {}
        for slot in sorted(
            slots,
            key=lambda s: (*reversed(slot_identity(s)), s.sameStartBooked),
        ):
            choices.setdefault(slot_identity(slot), slot)
        for slot in choices.values():
            ref = base = slot_reference(slot)
            collision = 1
            while ref in self._slots:
                collision += 1
                ref = f"{base}-{collision}"
            self._slots[ref] = OfferedSlot(
                slot, key.context, key.office, key.visit, expiry
            )
            self._issued[ref] = generation
        answer = reply(
            "found",
            "success: Found eligible openings.",
            searchedFrom=first,
            searchedThrough=through,
            slots=[
                {
                    "appointmentSlotRef": ref,
                    "datetime": item.slot.datetime,
                    "provider": provider_name(item.slot.provider),
                    "shared": bool(item.slot.sameStartBooked),
                }
                for ref, item in self._slots.items()
            ],
            soonest=soonest([(ref, item.slot) for ref, item in self._slots.items()]),
        )
        return self._remember(answer, expiry)

    def _remember(self, answer: dict, expires: datetime | None = None) -> dict:
        ttl = self._now() + timedelta(seconds=60)
        self._cache = (ttl if expires is None else min(expires, ttl), answer)
        return answer
