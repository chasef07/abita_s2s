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

    def _start_eligibility(self, details: EligibilityInput) -> EligibilityCheck | str:
        if self._closed:
            return "unavailable: Call is closing."
        if not parse_dob(details.dob):
            return "needs_input: Supply the patient's date of birth as MM/DD/YYYY."
        if normalize(details.plan) == "self pay":
            self.state.insurance.current_eligibility = None
            return "skipped: Self-pay does not require eligibility."
        if self.state.patient.active and not self._resolver.begin_registration(
            details.firstName, details.dob
        ):
            self.state.insurance.current_eligibility = None
            return "skipped: Eligibility checks are only for new-patient intake."
        office = self.state.call.called_office_key
        for check in self.state.insurance.eligibility_checks:
            if check.office == office and check.request == details:
                return check
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
        return check

    async def eligibility(self, details: EligibilityInput) -> str:
        check = self._start_eligibility(details)
        if isinstance(check, str):
            return check
        insurance = self.state.insurance
        revision = self.state.patient.revision
        accepted = accepted_insurance(self.state, details.coverageType)
        if accepted and normalize(details.plan) in {
            normalize(accepted.requested_plan),
            normalize(accepted.decision.canonicalPlan),
        }:
            check.canonical_plan = accepted.decision.canonicalPlan
        current = insurance.current_eligibility
        if current is None or current[0] != revision or current[1] is not check:
            if current and current[1].result and current[1].result.insuranceResolution:
                insurance.accepted = None
                insurance.check_revision += 1
            current = insurance.current_eligibility = (revision, check)
        if check.task is not None:
            await asyncio.shield(check.task)
        if (
            self.state.patient.revision != revision
            or self.state.call.called_office_key != check.office
            or insurance.current_eligibility is not current
        ):
            return "stale: Intake changed. Do not apply this eligibility result to the current patient."
        result = check.result
        if result is None:
            return "unavailable: Eligibility could not be confirmed. Continue intake without claiming coverage."
        plan_answer = self._apply_eligibility_insurance(check)
        if plan_answer and plan_answer["outcome"] != "accepted":
            return plan_answer["answer"]
        if person := result.name_correction:
            return (
                f"name_correction: Eligibility matched member ID and DOB. Use firstName={person.firstName!r}, lastName={person.lastName!r} "
                "in the patient's read-back before registration. If the patient is the policyholder, use that name for subscriberName too. "
                "Obtain confirmation of the corrected name before add_patient. Do not repeat eligibility just for this returned spelling. "
                f"Plan activity: {result.status}; this does not establish visit coverage or office participation."
            )
        plan_note = (
            f" Registration plan: {check.canonical_plan}." if plan_answer else ""
        )
        return f"eligibility: {result.status}. Review reason: {result.reviewReason or 'none'}.{plan_note} Continue intake; do not infer visit coverage."

    def _apply_eligibility_insurance(self, check: EligibilityCheck) -> dict | None:
        resolution = check.result.insuranceResolution if check.result else None
        if resolution is None or resolution.status == "unavailable":
            return None
        insurance = self.state.insurance
        insurance.accepted = None
        insurance.check_revision += 1
        decision = resolution.decision
        if (
            resolution.status != "resolved"
            or decision is None
            or decision.officeId.replace("_", "-") != check.office
            or decision.coverageType != check.request.coverageType
            or decision.selfPay
        ):
            return reply(
                "needs_insurance_review",
                "blocked: Eligibility returned an unmapped or conflicting insurance plan. Office staff must confirm the correct plan before registration. Do not reuse the earlier insurance selection.",
            )
        if decision.participation == "accepted":
            patient = self.state.patient
            insurance.accepted = AcceptedInsurance(
                check.office,
                patient.revision,
                None,
                patient.absence,
                decision,
                requested_plan=check.request.plan,
            )
            check.canonical_plan = decision.canonicalPlan
        return reply(decision.outcome, decision.answer)

    def _registration_eligibility(
        self, r: Registration, checked: AcceptedInsurance
    ) -> EligibilityCheck | None:
        current = self.state.insurance.current_eligibility
        if current is None or current[0] != self.state.patient.revision:
            return None
        check = current[1]
        if (
            check.office != self.state.call.called_office_key
            or check.request.coverageType != checked.decision.coverageType
            or not dob_matches(check.request.dob, r.dob)
            or normalize(check.canonical_plan or check.request.plan)
            not in {
                normalize(checked.requested_plan),
                normalize(checked.decision.canonicalPlan),
            }
            or "".join(check.request.memberId.split()).upper()
            != "".join(r.insuranceMemberId.split()).upper()
        ):
            return None
        return check

    def _eligibility_name_blocker(
        self, r: Registration, checked: AcceptedInsurance
    ) -> dict | None:
        check = self._registration_eligibility(r, checked)
        if check is None:
            return None
        if check.status == "pending":
            return reply(
                "eligibility_pending",
                "needs_input: Finish the existing check_new_patient_eligibility before confirming the registration name. Reuse the same inputs; it will not send another request.",
            )
        person = check.result.name_correction if check.result else None
        if person is None:
            return None
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
        current = self.state.insurance.current_eligibility
        if (
            current
            and current[0] == self.state.patient.revision
            and self.state.patient.active is None
        ):
            check = current[1]
            resolution = check.result.insuranceResolution if check.result else None
            if resolution and resolution.status != "unavailable":
                if coverage_type != check.request.coverageType or normalize(
                    plan
                ) not in {
                    normalize(check.request.plan),
                    normalize(check.canonical_plan or ""),
                    *(normalize(p) for p in resolution.plans),
                }:
                    self.state.insurance.accepted = None
                    self.state.insurance.check_revision += 1
                    return reply(
                        "needs_eligibility",
                        "needs_input: Insurance changed after eligibility. Check eligibility for the new plan and member ID before registration.",
                    )
                return self._apply_eligibility_insurance(check)
        if current and normalize(plan) not in {
            normalize(current[1].request.plan),
            normalize(current[1].canonical_plan or ""),
        }:
            self.state.insurance.current_eligibility = None
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
                requested_plan=plan,
            )
            if (
                current is not None
                and self.state.insurance.current_eligibility is current
                and current[0] == revision
                and current[1].request.coverageType == coverage_type
            ):
                current[1].canonical_plan = decision.canonicalPlan
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
        current = self.state.insurance.current_eligibility
        if current and current[0] == self.state.patient.revision:
            check = current[1]
            if check.status == "pending":
                return reply(
                    "eligibility_pending",
                    "needs_input: Finish the existing eligibility check before registration.",
                )
            resolution = check.result.insuranceResolution if check.result else None
            if resolution and resolution.status != "unavailable":
                person = check.result.name_correction or check.request
                if (
                    check.office != self.state.call.called_office_key
                    or not dob_matches(check.request.dob, r.dob)
                    or "".join(check.request.memberId.split()).upper()
                    != "".join(r.insuranceMemberId.split()).upper()
                    or exact_name(f"{r.firstName} {r.lastName}")
                    not in {
                        exact_name(
                            f"{check.request.firstName} {check.request.lastName}"
                        ),
                        exact_name(f"{person.firstName} {person.lastName}"),
                    }
                ):
                    return reply(
                        "needs_eligibility",
                        "needs_input: Registration details differ from the eligibility check. Check eligibility for this patient and member ID before registration.",
                    )
                # Do not recreate acceptance here: a later plan change clears it.
                if (
                    resolution.status != "resolved"
                    or not resolution.decision
                    or resolution.decision.participation != "accepted"
                ):
                    return self._apply_eligibility_insurance(check)
        checked = accepted_insurance(self.state)
        if checked and current and current[0] == self.state.patient.revision:
            resolution = (
                current[1].result.insuranceResolution if current[1].result else None
            )
            if (
                resolution
                and resolution.status == "resolved"
                and checked.decision != resolution.decision
            ):
                return reply(
                    "needs_eligibility",
                    "needs_input: Finish the existing eligibility check so registration uses its resolved insurance plan.",
                )
        if checked is None:
            return reply(
                "needs_insurance",
                "needs_input: Check accepted coverage for this patient and the intended medical or routine vision visit, then call add_patient again.",
            )
        if checked.decision.participation != "accepted":
            return reply(checked.decision.outcome, checked.decision.answer)
        self_pay = checked.decision.selfPay
        if not self_pay and (blocked := self._eligibility_name_blocker(r, checked)):
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

        eligibility = (
            self._registration_eligibility(r, checked) if not self_pay else None
        )

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
                answer = self._created(result, r, checked, eligibility)
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

    def _created(
        self, result: CreationReceipt, r: Registration, checked, eligibility=None
    ) -> dict:
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
        if activated and result.status == "created" and eligibility is not None:
            person = (
                eligibility.result.name_correction if eligibility.result else None
            ) or eligibility.request
            if exact_name(f"{person.firstName} {person.lastName}") == exact_name(
                f"{r.firstName} {r.lastName}"
            ):
                eligibility.patient_id = result.patientId
                eligibility.canonical_plan = checked.decision.canonicalPlan
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
            # An attempted insurance replacement invalidates future appointment
            # links even if a later plan label returns to the old value.
            for check in self.state.insurance.eligibility_checks:
                if check.patient_id == active.patientId:
                    check.invalidated = True
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
