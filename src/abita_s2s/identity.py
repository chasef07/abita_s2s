"""One per-call owner for identity evidence, resolution, and patient changes."""

import asyncio

from abita_s2s.middleware import (
    Candidate,
    Candidates,
    Multiple,
    NotFound,
    PatientMiddleware,
    Receipt,
)
from abita_s2s.name_matcher import (
    dob_matches,
    exact_name,
    first_names,
    names_match,
    parse_dob,
    phone_name_matches,
)
from abita_s2s.state import (
    CallState,
    CandidateLookup,
    PatientAbsence,
)


def candidate_first_name(candidate: Candidate | Receipt) -> str:
    if isinstance(candidate, Candidate):
        return candidate.firstName
    names = first_names(candidate.name)
    return names[0] if names else ""


def reply(outcome: str, answer: str, next_input: str | None = None) -> dict:
    return {"outcome": outcome, "answer": answer, "next_input": next_input}


def failed() -> dict:
    return reply(
        "lookup_failed",
        "The patient lookup could not be verified. This does not mean the patient is new. Connect the caller to office staff.",
    )


def ambiguous(dob: str | None) -> dict:
    return reply(
        "multiple_matches",
        "What is the patient's date of birth?"
        if not dob
        else "These details match more than one patient. Clarify the first-name spelling and DOB; if still unresolved, connect the caller to office staff.",
        "dob" if not dob else "staff_help",
    )


class PatientResolver:
    def __init__(self, state: CallState, middleware: PatientMiddleware):
        self.state = state
        self._middleware = middleware
        self._pending: tuple[str | None, str | None] = (None, None)
        self._task: asyncio.Task | None = None
        self._precall: asyncio.Task | None = None
        self._closed = False
        self._previous_id: str | None = None
        self._token: object | None = None

    def _begin_lookup(self) -> object:
        self._token = object()
        self.state.patient.absence = None
        return self._token

    def _apply_lookup(self, token: object, lookup: CandidateLookup) -> None:
        if self._current(token):
            self._token = None
            self.state.patient.lookup = lookup

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

    async def _lookup_phone(self, token: object) -> None:
        result = await self._middleware.resolve(
            self.state.call.called_office_key, {"phone": self.state.call.caller_phone}
        )
        if isinstance(result, Receipt):
            matches = [result]
        elif isinstance(result, Multiple):
            matches = result.matches
        else:
            lookup = (
                CandidateLookup("none")
                if isinstance(result, NotFound)
                else CandidateLookup("failed", failure_reason="lookup_failed")
            )
            self._apply_lookup(token, lookup)
            return
        if len({c.patientId for c in matches}) != len(matches) or any(
            not candidate_first_name(c) for c in matches
        ):
            lookup = CandidateLookup("failed", failure_reason="invalid_response")
        else:
            lookup = CandidateLookup("found", tuple(matches))
        self._apply_lookup(token, lookup)

    async def aclose(self) -> None:
        self._closed = True
        self._token = None
        tasks = [t for t in (self._task, self._precall) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def resolve(self, first_name: str | None, dob: str | None) -> dict:
        if self._closed:
            return reply("superseded", "This call has ended.")
        first_name = first_name.strip() if first_name is not None else None
        dob = dob.strip() if dob is not None else None
        # A changed name starts fresh; a DOB-only followup keeps the pending name.
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
            and self._task is not None
            and not self._task.done()
        ):
            return await self._await_resolution(self._task, self._token)
        self._pending = (first_name, dob)
        active = self.state.patient.active
        if active and (
            (
                first_name
                and not any(
                    names_match(first_name, n) for n in first_names(active.name)
                )
            )
            or (dob and not dob_matches(dob, active.dob))
        ):
            self._previous_id = active.patientId
            self.state.patient.active = None
            self.state.patient.revision += 1
        token = self._begin_lookup()
        for task in (self._task, self._precall):
            if task is not None and not task.done():
                task.cancel()
        self._task = asyncio.create_task(self._resolve(first_name, dob, token))
        return await self._await_resolution(self._task, token)

    async def _await_resolution(self, task: asyncio.Task, token: object) -> dict:
        try:
            # Fence caller cancellation before cancelling the read, even if a transport
            # delays cancellation or returns a late result.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if self._token is not token:
                return reply(
                    "superseded", "Patient details changed; use the latest resolution."
                )
            self._token = None
            task.cancel()
            raise
        finally:
            if self._token is token:
                self._token = None

    def _current(self, token: object) -> bool:
        return not self._closed and self._token is not None and self._token is token

    async def _resolve(self, name: str | None, dob: str | None, token: object) -> dict:
        if not name or not exact_name(name):
            return reply(
                "needs_identity", "What is the patient's first name?", "firstName"
            )
        if dob is not None and not parse_dob(dob):
            return reply(
                "needs_identity",
                "Ask for a corrected date of birth in MM/DD/YYYY.",
                "dob",
            )
        selected = [
            c
            for c in self.state.patient.lookup.candidates
            if phone_name_matches(name, candidate_first_name(c))
            and (not dob or dob_matches(dob, c.dob))
        ]
        if len(selected) > 1:
            return ambiguous(dob)
        if selected:
            return await self._load_patient(selected[0], name, dob, token, phone=True)
        active = self.state.patient.active
        if (
            active
            and any(names_match(name, n) for n in first_names(active.name))
            and (not dob or dob_matches(dob, active.dob))
        ):
            if active.appointmentsStatus != "error":
                self._pending = (None, None)
                return self._facts(active, "verified")
            return await self._load_patient(active, name, dob, token, phone=True)
        if not dob:
            return reply(
                "needs_identity", "What is the patient's date of birth?", "dob"
            )
        result = await self._middleware.resolve(
            self.state.call.called_office_key, {"firstName": name, "dob": dob}
        )
        if not self._current(token):
            return reply(
                "superseded", "Patient details changed; use the latest resolution."
            )
        if not isinstance(result, Candidates) or not result.complete:
            return failed()
        if len({c.patientId for c in result.matches}) != len(result.matches):
            return failed()
        matches = [
            c
            for c in result.matches
            if exact_name(name) == exact_name(c.firstName) and dob_matches(dob, c.dob)
        ]
        if not matches:
            self.state.patient.absence = PatientAbsence(
                name, dob, self.state.call.called_office_key
            )
            return reply(
                "not_found",
                "A complete search found no matching patient. Clarify the first-name spelling and DOB; if still unresolved, ask office staff for help.",
                "staff_help",
            )
        if len(matches) > 1:
            return ambiguous(dob)
        return await self._load_patient(matches[0], name, dob, token, phone=False)

    async def _load_patient(
        self,
        candidate: Candidate | Receipt,
        name: str,
        dob: str | None,
        token: object,
        *,
        phone: bool,
    ) -> dict:
        if not self._current(token):
            return reply(
                "superseded", "Patient details changed; use the latest resolution."
            )
        active = self.state.patient.active
        receipt = (
            active if active and active.patientId == candidate.patientId else candidate
        )
        if not isinstance(receipt, Receipt) or receipt.appointmentsStatus == "error":
            receipt = await self._middleware.resolve(
                self.state.call.called_office_key, {"patientId": candidate.patientId}
            )
        if not self._current(token):
            return reply(
                "superseded", "Patient details changed; use the latest resolution."
            )
        matcher = (
            phone_name_matches if phone else lambda a, b: exact_name(a) == exact_name(b)
        )
        if (
            not isinstance(receipt, Receipt)
            or receipt.patientId != candidate.patientId
            or not any(matcher(name, n) for n in first_names(receipt.name))
            or (dob and not dob_matches(dob, receipt.dob))
        ):
            return failed()
        previous = self.state.patient.active
        previous_id = previous.patientId if previous else self._previous_id
        switched = previous_id not in (None, receipt.patientId)
        self._token = None
        if previous is None or previous.patientId != receipt.patientId:
            self.state.patient.revision += 1
        self.state.patient.active = receipt
        self.state.patient.absence = None
        self._pending = (None, None)
        self._previous_id = None
        return self._facts(receipt, "switched" if switched else "verified")

    async def read_insurance(self, expected: Receipt) -> Receipt | None:
        """Reload private backend references without replacing current appointment state."""
        if self._closed or self.state.patient.active is not expected:
            return None
        token = self._begin_lookup()
        try:
            receipt = await self._middleware.resolve(
                self.state.call.called_office_key, {"patientId": expected.patientId}
            )
            if (
                not self._current(token)
                or self.state.patient.active is not expected
                or not isinstance(receipt, Receipt)
                or receipt.patientId != expected.patientId
                or exact_name(receipt.name) != exact_name(expected.name)
                or not dob_matches(receipt.dob, expected.dob)
            ):
                return None
            return receipt
        finally:
            if self._token is token:
                self._token = None

    def refresh_insurance(self, expected: Receipt, updated: Receipt) -> bool:
        """Apply validated coverage to the same patient, fencing older patient reads."""
        if (
            self._closed
            or self.state.patient.active is not expected
            or updated.patientId != expected.patientId
            or updated.name != expected.name
            or updated.dob != expected.dob
        ):
            return False
        self._token = None
        self.state.patient.active = updated
        self.state.patient.revision += 1
        return True

    def activate_created(self, absence: PatientAbsence, receipt: Receipt) -> bool:
        """Promote a validated creation receipt only for its still-current absence."""
        if (
            self._closed
            or self.state.patient.active is not None
            or self.state.patient.absence is not absence
            or absence.office_key != self.state.call.called_office_key
            or not dob_matches(absence.dob, receipt.dob)
            or not any(
                exact_name(absence.first_name) == exact_name(n)
                for n in first_names(receipt.name)
            )
        ):
            return False
        self._token = None
        self.state.patient.active = receipt
        self.state.patient.absence = None
        self.state.patient.revision += 1
        self._pending = (None, None)
        self._previous_id = None
        return True

    def _facts(self, receipt: Receipt, outcome: str) -> dict:
        result = reply(
            outcome,
            f"I found the patient record for {receipt.name}. DOB is on file; do not ask for DOB.",
        )
        result["patient"] = {
            "name": receipt.name,
            "dob_on_file": True,
            "insurance_on_file": receipt.insuranceCarrier,
            "appointments_status": receipt.appointmentsStatus,
        }
        if receipt.appointmentsStatus == "error":
            result["answer"] += (
                " Upcoming appointments could not be loaded; retry resolution to reload."
            )
        return result
