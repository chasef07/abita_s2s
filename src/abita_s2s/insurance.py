"""Per-call participation, registration and insurance-write owner."""

import asyncio
import re
from typing import Literal

from pydantic import ConfigDict

from abita_s2s.eligibility_contract import EligibilityCheck, EligibilityInput
from abita_s2s.identity import PatientResolver, reply
from abita_s2s.insurance_state import (
    AcceptedInsurance,
    CoverageType,
    accepted_insurance,
)
from abita_s2s.integrations.patient_middleware import Receipt, Record
from abita_s2s.name_matcher import dob_matches, exact_name, parse_dob
from abita_s2s.integrations.registration_middleware import (
    CreationReceipt,
    RegistrationMiddleware,
    UpdatedReceipt,
    WriteFailure,
)
from abita_s2s.state import CallState


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold().replace("&", " and ")).strip()


class Registration(Record):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", str_strip_whitespace=True
    )
    firstName: str
    lastName: str
    dob: str
    phone: str | None
    inboundPhoneConfirmed: Literal[True] | None
    email: str | None
    street: str
    aptSuite: str | None
    city: str
    state: str
    zip: str
    sex: Literal["male", "female"]
    subscriberName: str
    insuranceMemberId: str
    readBack: Literal[True] | None


def staff(outcome="needs_staff_review"):
    return reply(
        outcome,
        "blocked: Office staff must verify the registration or insurance result before continuing. Do not repeat this write.",
    )


class InsuranceRegistration:
    def __init__(
        self,
        state: CallState,
        resolver: PatientResolver,
        middleware: RegistrationMiddleware,
    ):
        self.state = state
        self._resolver = resolver
        self._middleware = middleware
        # Call-local receipts survive switches and repeat calls; never model-visible IDs.
        self._creations: list[tuple[tuple, dict]] = []
        self._updates: list[tuple[tuple, dict]] = []
        self._task: asyncio.Task | None = None
        self._closed = False

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self):
        self._closed = True
        tasks = [
            c.task
            for c in self.state.insurance.eligibility_checks
            if c.task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks)
        if self._task:
            await asyncio.shield(self._task)

    def start_eligibility(self, details: EligibilityInput) -> str:
        if self._closed:
            return "unavailable: Call is closing."
        if not parse_dob(details.dob):
            return "needs_input: Supply the patient's date of birth as MM/DD/YYYY."
        active = self.state.patient.active
        if active and (
            active.patientId not in self.state.insurance.registrations
            or (
                dob_matches(active.dob, details.dob)
                and exact_name(active.name)
                in {
                    exact_name(f"{details.firstName} {details.lastName}"),
                    exact_name(f"{details.lastName} {details.firstName}"),
                }
            )
        ):
            return "skipped: Eligibility checks are only for new-patient intake."
        if normalize(details.plan) == "self pay":
            return "skipped: Self-pay does not require eligibility."
        office = self.state.call.called_office_key
        for check in self.state.insurance.eligibility_checks:
            if check.office == office and check.request == details:
                return (
                    "already_started: Continue intake; do not wait or repeat the check."
                )
        check = EligibilityCheck(office=office, request=details)
        self.state.insurance.eligibility_checks.append(check)

        async def run():
            try:
                check.result = await self._middleware.eligibility(office, details)
                check.status = "complete" if check.result is not None else "unavailable"
                if check.result is None:
                    check.failure_reason = "unverified_response"
            except asyncio.CancelledError:
                check.status = "unavailable"
                check.failure_reason = "cancelled"
                raise
            except Exception:
                check.status = "unavailable"
                check.failure_reason = "request_failed"

        check.task = asyncio.create_task(run())
        return "started: Continue intake without waiting. Results are stored internally; do not announce coverage."

    async def eligibility(self, details: EligibilityInput) -> str:
        revision = self.state.patient.revision
        answer = self.start_eligibility(details)
        if not answer.startswith(("started:", "already_started:")):
            return answer
        check = next(
            c
            for c in self.state.insurance.eligibility_checks
            if c.office == self.state.call.called_office_key and c.request == details
        )
        if check.task is not None:
            await asyncio.shield(check.task)
        if (
            self.state.patient.revision != revision
            or self.state.call.called_office_key != check.office
            or self.state.insurance.eligibility_checks[-1] is not check
        ):
            return "stale: Intake changed. Do not apply this eligibility result to the current patient."
        result = check.result
        if result is None:
            return "unavailable: Eligibility could not be confirmed. Continue intake without claiming coverage."
        if (
            result.identity
            and result.identity.status == "matched_with_name_correction"
            and not result.identity.reviewRequired
            and result.matchedPatient
        ):
            person = result.matchedPatient
            return (
                f"name_correction: Eligibility matched member ID and DOB. Use firstName={person.firstName!r}, lastName={person.lastName!r} "
                "in the patient's read-back before registration. If the patient is the policyholder, use that name for subscriberName too. "
                "Obtain confirmation of the corrected name before add_patient. Do not repeat eligibility just for this returned spelling. "
                f"Plan activity: {result.status}; this does not establish visit coverage or office participation."
            )
        return f"eligibility: {result.status}. Review reason: {result.reviewReason or 'none'}. Continue intake; do not infer visit coverage."

    def _eligibility_name_blocker(self, r: Registration, plan: str) -> dict | None:
        checks = self.state.insurance.eligibility_checks
        if not checks:
            return None
        check = checks[-1]
        if (
            check.office != self.state.call.called_office_key
            or not dob_matches(check.request.dob, r.dob)
            or normalize(check.request.plan) != normalize(plan)
            or "".join(check.request.memberId.split()).upper()
            != "".join(r.insuranceMemberId.split()).upper()
        ):
            return None
        if check.status == "pending":
            return reply(
                "eligibility_pending",
                "needs_input: Finish the existing check_new_patient_eligibility before confirming the registration name. Reuse the same inputs; it will not send another request.",
            )
        result = check.result
        if (
            not result
            or not result.identity
            or result.identity.reviewRequired
            or result.identity.status != "matched_with_name_correction"
            or not result.matchedPatient
        ):
            return None
        person = result.matchedPatient
        original = exact_name(f"{check.request.firstName} {check.request.lastName}")
        corrected = exact_name(f"{person.firstName} {person.lastName}")
        supplied = exact_name(f"{r.firstName} {r.lastName}")
        if supplied not in (original, corrected):
            return None
        if supplied != corrected or (
            original != corrected and exact_name(r.subscriberName) == original
        ):
            return reply(
                "needs_name_confirmation",
                f"needs_input: Eligibility returned {person.firstName} {person.lastName} for the matching member ID and DOB. "
                "Confirm this name with the caller, then pass the corrected firstName and lastName to add_patient. "
                "Correct subscriberName too only if the patient is the policyholder; keep a different policyholder's name.",
            )
        return None

    async def check(self, plan: str, coverage_type: CoverageType) -> dict:
        self.state.insurance.accepted = None
        self.state.insurance.check_revision += 1
        check_revision = self.state.insurance.check_revision
        patient = self.state.patient
        revision, active, absence = patient.revision, patient.active, patient.absence
        office = self.state.call.called_office_key
        decision = await self._middleware.check(
            office, plan.strip(), coverage_type, active.dob if active else ""
        )
        if (
            patient.revision != revision
            or self.state.call.called_office_key != office
            or self.state.insurance.check_revision != check_revision
        ):
            return reply(
                "stale",
                "blocked: Patient or insurance context changed. Check insurance again.",
            )
        if decision is None:
            return reply(
                "unavailable",
                "blocked: Insurance participation could not be checked. Ask office staff for help.",
            )
        if decision.participation == "accepted" and decision.canonicalPlan:
            self.state.insurance.accepted = AcceptedInsurance(
                office,
                revision,
                active.patientId if active else None,
                absence,
                decision,
            )
        return reply(decision.outcome, decision.answer)

    def _write_blocker(self) -> dict | None:
        if self._closed:
            return staff()
        if self.state.insurance.write_pending:
            return reply(
                "write_pending",
                "blocked: A registration or insurance write is still in progress. Wait for its result.",
            )
        if self.state.insurance.write_uncertain:
            return staff("uncertain")
        return None

    async def _run_write(self, checked: AcceptedInsurance, work):
        if blocked := self._write_blocker():
            return blocked
        self.state.insurance.write_pending = True

        async def run():
            try:
                if accepted_insurance(self.state) is not checked:
                    return reply(
                        "stale",
                        "blocked: Patient or insurance context changed. No registration or insurance change was sent. Check insurance again.",
                    )
                return await work()
            except BaseException:
                self.state.insurance.write_uncertain = True
                raise
            finally:
                self.state.insurance.write_pending = False

        self._task = asyncio.create_task(run())
        return await asyncio.shield(self._task)

    async def add(
        self, registration: Registration, *, call_id: str | None = None
    ) -> dict:
        r = registration
        key = (
            self.state.call.called_office_key,
            exact_name(r.firstName),
            parse_dob(r.dob),
        )
        for prior, result in self._creations:
            if prior == key:
                return result
        if blocked := self._write_blocker():
            return blocked
        if self.state.patient.active and not self._resolver.begin_registration(
            r.firstName, r.dob
        ):
            return reply(
                "already_active",
                "blocked: A verified patient is already active. Do not create another chart for the same patient. For a different new patient, provide their first name and valid DOB.",
            )
        checked = accepted_insurance(self.state)
        if checked is None:
            return reply(
                "needs_insurance",
                "needs_input: Check accepted coverage for this patient and the intended medical or routine vision visit, then call add_patient again.",
            )
        if checked.decision.participation != "accepted":
            return reply(checked.decision.outcome, checked.decision.answer)
        self_pay = checked.decision.selfPay
        if not self_pay and (
            blocked := self._eligibility_name_blocker(r, checked.decision.canonicalPlan)
        ):
            return blocked
        phone = r.phone or (
            self.state.call.caller_phone if r.inboundPhoneConfirmed else None
        )
        if not phone:
            return reply(
                "needs_callback",
                "needs_input: Confirm the inbound number is a good callback number, or ask for a callback number for the patient.",
            )
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return reply(
                "needs_callback", "needs_input: Ask for a valid callback phone number."
            )
        if (
            not all((r.firstName, r.lastName, r.street, r.city, r.state, r.zip))
            or not parse_dob(r.dob)
            or not exact_name(r.firstName)
            or not exact_name(r.lastName)
            or not re.fullmatch(r"[A-Za-z]{2}", r.state)
            or (not self_pay and not all((r.subscriberName, r.insuranceMemberId)))
        ):
            return reply(
                "needs_registration_details",
                "needs_input: Collect the patient's full identity, address, and insurance policyholder and member ID, then confirm a full read-back.",
            )
        if not r.readBack:
            address = ", ".join(
                v for v in (r.street, r.aptSuite, r.city, r.state, r.zip) if v
            )
            coverage = (
                "The patient will use self-pay."
                if self_pay
                else f"Coverage is {checked.decision.canonicalPlan}, policyholder {r.subscriberName}, member ID {r.insuranceMemberId}."
            )
            return reply(
                "needs_read_back",
                f"needs_input: Confirm {r.firstName} {r.lastName}, DOB {r.dob}, {r.sex}; address {address}; callback {digits}"
                + (f"; email {r.email}" if r.email else "")
                + f". {coverage} Is all of that correct?",
            )
        payload = {
            k: getattr(r, k)
            for k in (
                "firstName",
                "lastName",
                "dob",
                "street",
                "city",
                "state",
                "zip",
                "sex",
            )
        }
        payload.update(
            phone=digits,
            aptSuite=r.aptSuite or "",
            insurance=checked.decision.canonicalPlan,
            subscriberName=r.subscriberName or f"{r.firstName} {r.lastName}",
            subscriberNum="self pay" if self_pay else r.insuranceMemberId,
        )
        if r.email:
            payload["email"] = r.email
        if checked.decision.coverageType == "routine_vision":
            payload["coverageType"] = "routine_vision"

        async def create():
            result = await self._middleware.create(checked.office_key, payload)
            if isinstance(result, WriteFailure):
                if result.status == "uncertain":
                    self.state.insurance.write_uncertain = True
                answer = (
                    reply(
                        "failed",
                        f"needs_input: Registration was not created ({result.reason}). Correct the details or resolve the failure before trying again.",
                    )
                    if result.status == "failed"
                    else staff(result.status)
                )
            else:
                answer = self._created(result, r, checked)
            if self.state.reporter:
                evidence = {"outcome": answer["outcome"]}
                if answer["outcome"] in ("created", "partial"):
                    evidence["externalPatientId"] = str(result.patientId)
                    active = self.state.patient.active
                    evidence["superseded"] = (
                        active is None or active.patientId != result.patientId
                    )
                self.state.reporter.record("patient", evidence, call_id=call_id)
            if not isinstance(result, WriteFailure) or result.status != "failed":
                self._creations.append((key, answer))
            return answer

        return await self._run_write(checked, create)

    def _created(self, result: CreationReceipt, r: Registration, checked) -> dict:
        # Validate both complete names without inventing backend identifier formats.
        expected = {
            exact_name(f"{r.firstName} {r.lastName}"),
            exact_name(f"{r.lastName} {r.firstName}"),
        }
        if exact_name(result.name) not in expected or not dob_matches(
            result.dob, r.dob
        ):
            self.state.insurance.write_uncertain = True
            return staff("invalid_receipt")
        patient = Receipt(
            status="verified",
            patientId=result.patientId,
            name=result.name,
            dob=result.dob,
            phone=r.phone or self.state.call.caller_phone,
            insuranceCarrier=checked.decision.canonicalPlan
            if result.status == "created"
            else None,
            # A complete creation confirms attachment of the accepted plan sent
            # with this write; the receipt need not repeat its decision.
            insuranceDecision=(result.insuranceDecision or checked.decision)
            if result.status == "created"
            else None,
            appointmentsStatus="none",
            appointments=[],
        )
        self.state.insurance.registrations[result.patientId] = result.status
        activated = self._resolver.activate_created(checked, patient)
        status = "success" if result.status == "created" and activated else "blocked"
        if result.status == "created":
            coverage = (
                "self-pay recorded"
                if checked.decision.selfPay
                else "insurance attached"
            )
            answer = f"{status}: New patient chart created with {coverage}."
        else:
            answer = f"{status}: Created the patient chart for {result.name}."
        if result.status == "partial":
            answer += " Insurance attachment is not confirmed. Office staff must finish registration; do not create another chart."
        if not activated:
            answer += f" The chart is for {result.name}. The patient context changed while this was running; this receipt was not applied to the current patient. Do not repeat chart creation."
        return reply(result.status, answer)

    async def update(self, member_id: str, *, call_id: str | None = None) -> dict:
        active = self.state.patient.active
        checked = accepted_insurance(self.state)
        if active is None:
            return reply(
                "needs_resolution", "Verify the patient before changing insurance."
            )
        if checked is None:
            return reply(
                "needs_insurance",
                "Check accepted coverage for this patient and visit type before changing insurance.",
            )
        if checked.decision.participation != "accepted":
            return reply(checked.decision.outcome, checked.decision.answer)
        member_id = "self pay" if checked.decision.selfPay else member_id.strip()
        if not member_id:
            return reply(
                "needs_member_id", "What is the member ID on the insurance card?"
            )
        key = (
            checked.office_key,
            active.patientId,
            checked.decision.canonicalPlan,
            checked.decision.coverageType,
            member_id,
        )
        for prior, result in reversed(self._updates):
            if prior[:2] == key[:2]:
                if result["outcome"] == "partial":
                    return result
                if prior == key and normalize(
                    active.insuranceCarrier or ""
                ) == normalize(checked.decision.canonicalPlan):
                    return result
                break

        async def update():
            payload = {
                "patientId": active.patientId,
                "dob": active.dob,
                "insurance": checked.decision.canonicalPlan,
                "coverageType": checked.decision.coverageType,
                "subscriberNum": member_id,
            }
            result = await self._middleware.update(checked.office_key, payload)
            if isinstance(result, WriteFailure):
                if result.status == "failed":
                    answer = reply(
                        "failed",
                        f"needs_input: Insurance was not changed ({result.reason}). Resolve the failure before trying again.",
                    )
                else:
                    self.state.insurance.write_uncertain = True
                    answer = (
                        reply(
                            "partial",
                            "blocked: The previous insurance plan was ended, but the replacement was not attached. Office staff must finish the change. Do not repeat this write.",
                        )
                        if result.status == "partial"
                        else staff(result.status)
                    )
            elif (
                not isinstance(result, UpdatedReceipt)
                or result.patientId != active.patientId
                or normalize(result.newInsurance)
                != normalize(checked.decision.canonicalPlan)
            ):
                self.state.insurance.write_uncertain = True
                answer = staff("uncertain")
            else:
                if active.patientId in self.state.insurance.registrations:
                    self.state.insurance.registrations[active.patientId] = "created"
                updated = active.model_copy(
                    update={
                        "insuranceCarrier": result.newInsurance,
                        "insuranceDecision": result.insuranceDecision
                        or checked.decision,
                    }
                )
                if self._resolver.refresh_insurance(active, updated, checked):
                    answer = reply(
                        "updated", f"Updated insurance to {result.newInsurance}."
                    )
                else:
                    answer = reply(
                        "updated",
                        f"Updated insurance for {active.name}. Patient context changed; the receipt was not applied to the current patient.",
                    )
            if self.state.reporter:
                self.state.reporter.record(
                    "insurance",
                    {
                        "outcome": answer["outcome"],
                        "externalPatientId": str(active.patientId),
                    },
                    call_id=call_id,
                )
            if not isinstance(result, WriteFailure) or result.status != "failed":
                self._updates.append((key, answer))
            return answer

        return await self._run_write(checked, update)
