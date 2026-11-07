"""One per-call owner for identity evidence, resolution, and patient changes."""

import asyncio
from dataclasses import dataclass

from abita_s2s.insurance_list import chart_plans
from abita_s2s.integrations.patient_middleware import (
    Multiple,
    NotFound,
    PatientMiddleware,
    ResolveRequest,
)
from abita_s2s.name_matcher import (
    dob_matches,
    exact_name,
    first_names,
    names_match,
    parse_dob,
    phone_name_matches,
    same_patient,
)
from abita_s2s.records import Candidate, Receipt
from abita_s2s.results import reply
from abita_s2s.state import CallState


def failed() -> dict:
    return reply(
        "lookup_failed",
        "blocked: The patient lookup could not be verified. This does not mean the patient is new. Connect the caller to office staff.",
    )


def superseded() -> dict:
    return reply(
        "superseded", "blocked: Patient details changed; use the latest resolution."
    )


def ambiguous(dob: str | None) -> dict:
    return reply(
        "multiple_matches",
        "needs_input: What is the patient's date of birth?"
        if not dob
        else "needs_input: These details match more than one patient. Clarify the first-name spelling and DOB; if still unresolved, connect the caller to office staff.",
    )


@dataclass(eq=False, repr=False)
class Lookup:
    """One patient read under way, and where the record ended when it started."""

    position: int


class PatientResolver:
    def __init__(self, state: CallState, middleware: PatientMiddleware):
        self.state = state
        self._middleware = middleware
        self._pending: tuple[str | None, str | None] = (None, None)
        self._task: asyncio.Task | None = None
        self._precall: asyncio.Task | None = None
        self._previous_id: str | None = None
        self._token: Lookup | None = None
        self._verified_as: tuple[str, str] | None = None
        self._phone_chart: Receipt | None = None

    @property
    def _closed(self) -> bool:
        return self.state.record.closed

    def _select(self, receipt: Receipt) -> None:
        self.state.patient.select(receipt.patientId, self.state.record.loaded(receipt))

    def staff_task_patient(self) -> dict[str, str] | None:
        """Snapshot current caller-reported identity without promoting it to verified."""
        name, dob = self._pending
        if name or dob:
            return {k: v for k, v in {"name": name, "dob": dob}.items() if v}
        active = self.state.chart()
        if active:
            return {"id": active.patientId, "name": active.name, "dob": active.dob}
        return None

    def _same_as_active(
        self, active: Receipt, name: str | None, dob: str | None
    ) -> bool:
        """Repeating the name that verified this chart, even fuzzily, keeps it active."""
        if name and self._verified_as == (active.patientId, exact_name(name)):
            return same_patient(None, dob, active.name, active.dob, names_match)
        return same_patient(name, dob, active.name, active.dob, names_match)

    def _begin_lookup(self) -> Lookup:
        self._token = Lookup(self.state.record.position())
        return self._token

    def start_phone_lookup(self) -> None:
        if (
            self._task is not None
            or self._closed
            or self._precall is not None
            or not self.state.call.caller_phone
        ):
            return
        token = self._begin_lookup()
        self._precall = asyncio.create_task(self._lookup_phone(token))

    async def _lookup_phone(self, token: Lookup) -> tuple[Candidate, ...]:
        """Identify the caller's charts; a single chart serves only the call's first activation."""
        result = await self._middleware.resolve(
            ResolveRequest(
                phone=self.state.call.caller_phone,
                office=self.state.call.office.key,
            )
        )
        matches: list[Candidate] = []
        if isinstance(result, Multiple):
            matches = result.matches
        elif isinstance(result, Receipt) and (names := first_names(result.name)):
            self._phone_chart = result
            matches = [
                Candidate(
                    status="candidate",
                    patientId=result.patientId,
                    firstName=names[0],
                    dob=result.dob,
                )
            ]
        if len({c.patientId for c in matches}) != len(matches):
            matches = []
        candidates = tuple(matches)
        if self._current(token):
            self._token = None
            self.state.patient.record_candidates(candidates)
        return candidates

    def close_admission(self) -> None:
        self.state.record.close()

    async def aclose(self) -> None:
        self.close_admission()
        self._token = None
        tasks = [t for t in (self._task, self._precall) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def resolve(
        self, first_name: str | None, dob: str | None, *, call_id: str | None = None
    ) -> dict:
        if self._closed:
            return reply("superseded", "blocked: This call has ended.")
        first_name = first_name.strip() if first_name is not None else None
        dob = dob.strip() if dob is not None else None
        previous_name, previous_dob = self._pending
        if first_name is not None and exact_name(first_name) != exact_name(
            previous_name or ""
        ):
            previous_dob = None
        first_name = first_name if first_name is not None else previous_name
        dob = dob if dob is not None else previous_dob
        key = (first_name, dob)
        if (
            self._pending == key
            and self._token is not None
            and self._current(self._token, self.state.patient.patient_id)
            and self._task is not None
            and not self._task.done()
        ):
            return await asyncio.shield(self._task)
        self._pending = (first_name, dob)
        active = self.state.chart()
        if active and not self._same_as_active(active, first_name, dob):
            self._previous_id = active.patientId
            self.state.patient.deselect()
        token = self._begin_lookup()
        if self._task is not None:
            self._task.cancel()

        async def lookup():
            try:
                if self._precall is not None:
                    candidates = await asyncio.shield(self._precall)
                    if not self._current(token):
                        return superseded()
                    self.state.patient.record_candidates(candidates)
                return await self._resolve(first_name, dob, token, call_id=call_id)
            except asyncio.CancelledError:
                if self._token is token:
                    raise
                return reply(
                    "superseded", "blocked: Patient details changed or this call ended."
                )
            finally:
                if self._token is token:
                    self._token = None

        self._task = asyncio.create_task(lookup())
        return await asyncio.shield(self._task)

    def _current(self, token: Lookup, patient_id: str | None = None) -> bool:
        """Whether this read is still the latest and, for a chart, nothing outdated it."""
        return (
            not self._closed
            and self._token is not None
            and self._token is token
            and not (
                patient_id and self.state.record.discards(token.position, patient_id)
            )
        )

    async def _resolve(
        self,
        name: str | None,
        dob: str | None,
        token: Lookup,
        *,
        call_id: str | None = None,
    ) -> dict:
        if not name or not exact_name(name):
            return reply(
                "needs_identity", "needs_input: What is the patient's first name?"
            )
        if dob is not None and not parse_dob(dob):
            return reply(
                "needs_identity",
                "needs_input: Ask for a corrected date of birth in MM/DD/YYYY.",
            )
        selected = [
            c
            for c in self.state.patient.candidates
            if phone_name_matches(name, c.firstName)
            and (not dob or dob_matches(dob, c.dob))
        ]
        active = self.state.chart()
        if (
            active
            and self._same_as_active(active, name, dob)
            and (dob or len(selected) < 2)
        ):
            if active.appointmentsStatus != "error":
                self._pending = (None, None)
                return self._facts(active, "verified", call_id=call_id)
            return await self._load_patient(
                active, name, dob, token, phone=True, call_id=call_id
            )
        if len(selected) > 1:
            return ambiguous(dob)
        if selected:
            return await self._load_patient(
                selected[0], name, dob, token, phone=True, call_id=call_id
            )
        if not dob:
            return reply(
                "needs_identity", "needs_input: What is the patient's date of birth?"
            )
        result = await self._middleware.resolve(
            ResolveRequest(firstName=name, dob=dob, office=self.state.call.office.key)
        )
        if not self._current(token):
            return superseded()
        if isinstance(result, NotFound):
            return reply(
                "not_found",
                "no_results: A complete search found no matching patient. Confirm the first-name spelling and DOB once and retry any correction; if still no match, tell the caller you'll set them up as a new patient and start New patient intake.",
            )
        if isinstance(result, Multiple):
            return ambiguous(dob)
        if not isinstance(result, Receipt):
            return failed()
        return await self._load_patient(
            result, name, dob, token, phone=False, call_id=call_id
        )

    async def _load_patient(
        self,
        candidate: Candidate | Receipt,
        name: str,
        dob: str | None,
        token: Lookup,
        *,
        phone: bool,
        call_id: str | None = None,
    ) -> dict:
        if not self._current(token, candidate.patientId):
            return superseded()
        chart = self._phone_chart
        if not phone:
            receipt = candidate
        elif (
            chart
            and chart.patientId == candidate.patientId
            and chart.appointmentsStatus != "error"
        ):
            receipt = chart
        else:
            receipt = await self._read_chart(candidate.patientId)
        if not self._current(token, candidate.patientId):
            return superseded()
        matcher = (
            phone_name_matches if phone else lambda a, b: exact_name(a) == exact_name(b)
        )
        if (
            not isinstance(receipt, Receipt)
            or receipt.patientId != candidate.patientId
            or not same_patient(name, dob, receipt.name, receipt.dob, matcher)
        ):
            return failed()
        previous_id = self.state.patient.patient_id or self._previous_id
        switched = previous_id not in (None, receipt.patientId)
        self._token = None
        self._phone_chart = None
        self._select(receipt)
        self._pending = (None, None)
        self._previous_id = None
        self._verified_as = (receipt.patientId, exact_name(name))
        return self._facts(
            self.state.chart() or receipt,
            "switched" if switched else "verified",
            call_id=call_id,
        )

    def unchanged_since(self, patient_id: str, since: int | None) -> bool:
        """Whether a write's patient is still the call's, without having left and come back."""
        patient = self.state.patient
        return patient.patient_id == patient_id and patient.since == since

    def _read_chart(self, patient_id: str):
        return self._middleware.resolve(
            ResolveRequest(patientId=patient_id, office=self.state.call.office.key)
        )

    async def reload_chart(self) -> bool:
        """Re-read the active chart for fresh appointment tokens; report whether it committed.

        A patient change, a settled write, or a revocation during the read discards it,
        and a read with no appointments leaves the chart revoked.
        """
        active = self.state.chart()
        if self._closed or active is None:
            return False
        token = self._begin_lookup()
        try:
            receipt = await self._read_chart(active.patientId)
            if (
                not self._current(token, active.patientId)
                or not isinstance(receipt, Receipt)
                or receipt.patientId != active.patientId
                or not dob_matches(receipt.dob, active.dob)
                or receipt.appointmentsStatus == "error"
            ):
                return False
            self._select(receipt)
            return True
        finally:
            if self._token is token:
                self._token = None

    def begin_registration(self, first_name: str, dob: str) -> bool:
        """Make the named new intake the pending patient, leaving a different patient's chart.

        Runs before the intake is validated or read back. A rejected intake can lead to a
        staff request, which must name this patient and not the chart the call was on.
        """
        active = self.state.chart()
        if (
            self._closed
            or not exact_name(first_name)
            or not parse_dob(dob)
            or (active and self._same_as_active(active, first_name, dob))
        ):
            return False
        self._token = None
        self._pending = (first_name, dob)
        self._previous_id = None
        self.state.patient.deselect()
        return True

    def registering(self, first_name: str, dob: str) -> bool:
        """Whether new intake for this identity is still the call's patient."""
        return (
            not self._closed
            and self.state.patient.patient_id is None
            and self._pending == (first_name, dob)
        )

    def activate_created(self, first_name: str, dob: str, receipt: Receipt) -> bool:
        """Commit a validated creation only while its intake is still the call's patient."""
        if not self.registering(first_name, dob):
            return False
        self._token = None
        self._phone_chart = None
        self._select(receipt)
        self._pending = (None, None)
        self._previous_id = None
        return True

    def _facts(
        self, receipt: Receipt, outcome: str, *, call_id: str | None = None
    ) -> dict:
        if self.state.reporter:
            registered = self.state.record.registration(receipt.patientId)
            for recorded in (outcome, registered) if registered else (outcome,):
                self.state.reporter.record(
                    "patient",
                    {"outcome": recorded, "externalPatientId": receipt.patientId},
                    call_id=call_id,
                )
        result = reply(
            outcome,
            f"success: I found the patient record for {receipt.name}. DOB is on file; do not ask for DOB."
            f" Insurance on file: {receipt.insuranceCarrier or 'none recorded'}."
            + chart_plans(
                self.state.plans,
                receipt.insuranceCarrierId,
                receipt.insuranceNote,
            ),
        )
        if receipt.appointmentsStatus == "error":
            result["answer"] += (
                " Upcoming appointments could not be loaded; retry resolution to reload."
            )
        return result
