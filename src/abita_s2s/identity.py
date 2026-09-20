"""One per-call owner for identity evidence, resolution, and patient changes."""

import asyncio
from dataclasses import replace

from abita_s2s.insurance_state import AcceptedInsurance, accepted_insurance
from abita_s2s.middleware import (
    Candidate,
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


def reply(outcome: str, answer: str) -> dict:
    return {"outcome": outcome, "answer": answer}


def failed() -> dict:
    return reply(
        "lookup_failed",
        "blocked: The patient lookup could not be verified. This does not mean the patient is new. Connect the caller to office staff.",
    )


def ambiguous(dob: str | None) -> dict:
    return reply(
        "multiple_matches",
        "needs_input: What is the patient's date of birth?"
        if not dob
        else "needs_input: These details match more than one patient. Clarify the first-name spelling and DOB; if still unresolved, connect the caller to office staff.",
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

    def staff_task_patient(self) -> dict[str, str] | None:
        """Snapshot current caller-reported identity without promoting it to verified."""
        name, dob = self._pending
        if name or dob:
            return {k: v for k, v in {"name": name, "dob": dob}.items() if v}
        active = self.state.patient.active
        if active:
            return {"id": active.patientId, "name": active.name, "dob": active.dob}
        return None

    def _begin_lookup(self) -> object:
        self._token = object()
        self.state.patient.absence = None
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

    async def _lookup_phone(self, token: object) -> CandidateLookup:
        result = await self._middleware.resolve(
            self.state.call.called_office_key, {"phone": self.state.call.caller_phone}
        )
        if isinstance(result, Receipt):
            matches = [result]
        elif isinstance(result, Multiple):
            matches = result.matches
        else:
            matches = []
        if not matches:
            lookup = (
                CandidateLookup("none")
                if isinstance(result, NotFound)
                else CandidateLookup("failed", failure_reason="lookup_failed")
            )
        elif len({c.patientId for c in matches}) != len(matches) or any(
            not candidate_first_name(c) for c in matches
        ):
            lookup = CandidateLookup("failed", failure_reason="invalid_response")
        else:
            lookup = CandidateLookup("found", tuple(matches))
        if self._current(token):
            self._token = None
            self.state.patient.lookup = lookup
        return lookup

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self) -> None:
        self._closed = True
        self._token = None
        tasks = [t for t in (self._task, self._precall) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def resolve(self, first_name: str | None, dob: str | None, *, call_id: str | None = None) -> dict:
        if self._closed:
            return reply("superseded", "blocked: This call has ended.")
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
            return await asyncio.shield(self._task)
        self._pending = (first_name, dob)
        checked = self.state.insurance.accepted
        if checked is not None and checked.patient_id is None:
            self.state.insurance.accepted = None
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
        if self._task is not None and not self._task.done():
            self._task.cancel()

        async def lookup():
            # The resolver owns completion; a cancelled waiter leaves the read running.
            try:
                if self._precall is not None:
                    candidates = await asyncio.shield(self._precall)
                    if not self._current(token):
                        return reply("superseded", "blocked: Patient details changed; use the latest resolution.")
                    self.state.patient.lookup = candidates
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

    def _current(self, token: object) -> bool:
        return not self._closed and self._token is not None and self._token is token

    async def _resolve(self, name: str | None, dob: str | None, token: object, *, call_id: str | None = None) -> dict:
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
            for c in self.state.patient.lookup.candidates
            if phone_name_matches(name, candidate_first_name(c))
            and (not dob or dob_matches(dob, c.dob))
        ]
        if len(selected) > 1:
            return ambiguous(dob)
        if selected:
            return await self._load_patient(selected[0], name, dob, token, phone=True, call_id=call_id)
        active = self.state.patient.active
        if (
            active
            and any(names_match(name, n) for n in first_names(active.name))
            and (not dob or dob_matches(dob, active.dob))
        ):
            if active.appointmentsStatus != "error":
                self._pending = (None, None)
                return self._facts(active, "verified", call_id=call_id)
            return await self._load_patient(active, name, dob, token, phone=True, call_id=call_id)
        if not dob:
            return reply(
                "needs_identity", "needs_input: What is the patient's date of birth?"
            )
        result = await self._middleware.resolve(
            self.state.call.called_office_key, {"firstName": name, "dob": dob}
        )
        if not self._current(token):
            return reply(
                "superseded", "blocked: Patient details changed; use the latest resolution."
            )
        if isinstance(result, NotFound):
            self.state.patient.absence = PatientAbsence(
                name, dob, self.state.call.called_office_key
            )
            return reply(
                "not_found",
                "no_results: A complete search found no matching patient. Clarify the first-name spelling and DOB; if still unresolved, ask office staff for help.",
            )
        if isinstance(result, Multiple):
            return ambiguous(dob)
        if not isinstance(result, Receipt):
            return failed()
        return await self._load_patient(result, name, dob, token, phone=False, call_id=call_id)

    async def _load_patient(
        self,
        candidate: Candidate | Receipt,
        name: str,
        dob: str | None,
        token: object,
        *,
        phone: bool,
        call_id: str | None = None,
    ) -> dict:
        if not self._current(token):
            return reply(
                "superseded", "blocked: Patient details changed; use the latest resolution."
            )
        active = self.state.patient.active
        receipt = (
            active if phone and active and active.patientId == candidate.patientId else candidate
        )
        if not isinstance(receipt, Receipt) or (phone and receipt.appointmentsStatus == "error"):
            receipt = await self._middleware.resolve(
                self.state.call.called_office_key, {"patientId": candidate.patientId}
            )
        if not self._current(token):
            return reply(
                "superseded", "blocked: Patient details changed; use the latest resolution."
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
        return self._facts(receipt, "switched" if switched else "verified", call_id=call_id)

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

    def refresh_insurance(
        self, expected: Receipt, updated: Receipt, checked: AcceptedInsurance
    ) -> bool:
        """Commit validated coverage and its acceptance, fencing older patient reads."""
        if (
            self._closed
            or self.state.patient.revision != checked.patient_revision
            or self.state.patient.active is not expected
            or updated.patientId != expected.patientId
            or updated.name != expected.name
            or updated.dob != expected.dob
        ):
            return False
        self._token = None
        self.state.patient.active = updated
        self.state.patient.revision += 1
        # A newer plan check must not be rebound to this older write's receipt.
        if self.state.insurance.accepted is checked:
            self.state.insurance.accepted = replace(
                checked, patient_revision=self.state.patient.revision,
                decision=updated.insuranceDecision,
            ) if updated.insuranceDecision else None
        return True

    def activate_created(self, checked: AcceptedInsurance, receipt: Receipt) -> bool:
        """Commit a validated creation only while its original acceptance is current."""
        if (
            self._closed
            or self.state.patient.active is not None
            or accepted_insurance(self.state) is not checked
        ):
            return False
        self._token = None
        self.state.patient.active = receipt
        self.state.patient.absence = None
        self.state.patient.revision += 1
        self._pending = (None, None)
        self._previous_id = None
        self.state.insurance.accepted = replace(
            checked,
            patient_revision=self.state.patient.revision,
            patient_id=receipt.patientId,
            absence=None,
            decision=receipt.insuranceDecision,
        ) if receipt.insuranceDecision else None
        return True

    def _facts(self, receipt: Receipt, outcome: str, *, call_id: str | None = None) -> dict:
        if self.state.reporter:
            self.state.reporter.record("patient", {
                "outcome": outcome, "externalPatientId": receipt.patientId,
            }, call_id=call_id)
            # Resolving a chart created in this call must not label it existing.
            if receipt.patientId in self.state.insurance.registrations:
                self.state.reporter.record("patient", {
                    "outcome": "created", "externalPatientId": receipt.patientId,
                }, call_id=call_id)
        result = reply(
            outcome,
            f"success: I found the patient record for {receipt.name}. DOB is on file; do not ask for DOB."
            f" Insurance on file: {receipt.insuranceCarrier or 'none recorded'}.",
        )
        if receipt.appointmentsStatus == "error":
            result["answer"] += (
                " Upcoming appointments could not be loaded; retry resolution to reload."
            )
        return result
