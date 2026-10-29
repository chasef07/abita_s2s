"""Per-call registration and insurance-write owner, and wording for middleware insurance decisions."""

import asyncio
import re
from collections.abc import Callable
from typing import Literal

from pydantic import ConfigDict

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance_contract import CoverageType, InsuranceDecision
from abita_s2s.insurance_list import VISITS, listed_plan
from abita_s2s.integrations.patient_middleware import Receipt
from abita_s2s.name_matcher import dob_matches, exact_name, parse_dob
from abita_s2s.offices import e164
from abita_s2s.records import Record
from abita_s2s.results import reply
from abita_s2s.integrations.registration_middleware import (
    CreationReceipt,
    InsuranceChange,
    NewPatient,
    PlanSummary,
    RegistrationMiddleware,
    WriteFailure,
)
from abita_s2s.state import CallState
from abita_s2s.writes import WriteKey, WriteLedger, WriteOutcome, write_effect


def full_name(person) -> str:
    return exact_name(f"{person.firstName} {person.lastName}")


class Registration(Record):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
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
    visitType: CoverageType
    insurancePlanId: str
    subscriberName: str
    insuranceMemberId: str
    readBack: Literal[True] | None


def staff(outcome="needs_staff_review"):
    return reply(
        outcome,
        "blocked: Office staff must verify the registration or insurance result before continuing. Do not repeat this write.",
    )


REQUIREMENTS = {
    "pcp_referral": "a referral from the patient's primary care doctor",
    "prior_authorization": "prior authorization",
    "staff_verify": "this plan's coverage",
}
ANSWERS = {
    "accepted": "{plan} is accepted for {visits} at this office.",
    "not_accepted": "{plan} is not accepted for {visits} at this office.",
    "office_no_coverage": "This office does not accept insurance for {visits}.",
    "no_self_pay": "This office does not offer self-pay for {visits}.",
    "ask_card": "Ask what insurance plan is on the card.",
    "ask_full_name": "Ask for the full plan name on the card.",
    "choose_plan": "Ask which plan is on the card: {options}.",
    "requirement": "Office staff must verify {requirement} before scheduling {visits}. {plan} is accepted.",
    "pending_confirmation": "Office staff must confirm {plan} for {visits} before scheduling.",
    "no_provider_for_age": "{plan} is accepted, but no doctor here who takes it can see a patient of this age. Office staff must arrange this visit.",
    "chart_unverified": "Office staff must verify the insurance on the chart before scheduling.",
}


def decision_answer(decision: InsuranceDecision) -> str:
    """Model-facing sentence for a participation decision, written here from its fields."""
    answer = ANSWERS[decision.reason].format(
        plan=decision.canonicalPlan,
        visits=VISITS[decision.coverageType],
        options=", ".join(option.label for option in decision.options),
        requirement=REQUIREMENTS[decision.requirements[0].kind]
        if decision.requirements
        else "",
    )
    if decision.callerNotice:
        answer += f" Note: {decision.callerNotice}"
    return answer


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
        self._ledger: WriteLedger[dict] = WriteLedger()
        self._task: asyncio.Task | None = None
        self._closed = False

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self):
        self._closed = True
        if self._task:
            await asyncio.shield(self._task)

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

    async def _run_write(self, current: Callable[[], bool], work):
        if blocked := self._write_blocker():
            return blocked
        self.state.insurance.write_pending = True

        async def run():
            try:
                if not current():
                    return reply(
                        "stale",
                        "blocked: The patient changed. No registration or insurance change was sent.",
                    )
                return await work()
            except BaseException:
                self.state.insurance.write_uncertain = True
                raise
            finally:
                self.state.insurance.write_pending = False

        self._task = asyncio.create_task(run())
        return await asyncio.shield(self._task)

    def _record(self, key: WriteKey, outcome: WriteOutcome, answer: dict) -> dict:
        if write_effect(outcome).blocks:
            self.state.insurance.write_uncertain = True
        self._ledger.record(key, outcome, answer)
        return answer

    async def add(
        self, registration: Registration, *, call_id: str | None = None
    ) -> dict:
        r = registration
        key = WriteKey(
            (
                self.state.call.called_office_key,
                exact_name(r.firstName),
                parse_dob(r.dob),
            ),
            "register",
        )
        if saved := self._ledger.get(key):
            return saved
        if blocked := self._write_blocker():
            return blocked
        if (
            not self._resolver.begin_registration(r.firstName, r.dob)
            and self.state.patient.active
        ):
            return reply(
                "already_active",
                "blocked: A verified patient is already active. Do not create another chart for the same patient. For a different new patient, provide their first name and valid DOB.",
            )
        plan = listed_plan(self.state, r.insurancePlanId, r.visitType)
        if isinstance(plan, dict):
            return plan
        self_pay = plan.selfPay
        phone = r.phone or (
            self.state.call.caller_phone if r.inboundPhoneConfirmed else None
        )
        if not phone:
            return reply(
                "needs_callback",
                "needs_input: Confirm the inbound number is a good callback number, or ask for a callback number for the patient.",
            )
        callback = e164(phone)
        if callback is None or not callback.startswith("+1") or len(callback) != 12:
            return reply(
                "needs_callback", "needs_input: Ask for a valid callback phone number."
            )
        digits = callback[2:]
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
                else f"Coverage is {plan.label}, policyholder {r.subscriberName}, member ID {r.insuranceMemberId}."
            )
            return reply(
                "needs_read_back",
                f"needs_input: Confirm {r.firstName} {r.lastName}, DOB {r.dob}, {r.sex}; address {address}; callback {digits}"
                + (f"; email {r.email}" if r.email else "")
                + f". {coverage} Is all of that correct?",
            )
        request = NewPatient(
            firstName=r.firstName,
            lastName=r.lastName,
            dob=r.dob,
            street=r.street,
            city=r.city,
            state=r.state,
            zip=r.zip,
            sex=r.sex,
            phone=digits,
            aptSuite=r.aptSuite or "",
            subscriberName=r.subscriberName or f"{r.firstName} {r.lastName}",
            subscriberNum="self pay" if self_pay else r.insuranceMemberId,
            insurancePlanId=plan.planId,
            email=r.email or None,
            coverageType=r.visitType,
            office=self.state.call.called_office_key,
        )

        async def create():
            result = await self._middleware.create(request)
            chart = None
            if isinstance(result, WriteFailure):
                outcome: WriteOutcome = result.status
                answer = (
                    reply(
                        "failed",
                        f"needs_input: Registration was not created ({result.reason}). Correct the details or resolve the failure before trying again.",
                    )
                    if result.status == "failed"
                    else staff(result.status)
                )
            else:
                outcome, answer = self._created(result, r, plan)
                if outcome == "written":
                    chart = result.patientId
            if self.state.reporter:
                evidence: dict[str, object] = {"outcome": answer["outcome"]}
                if chart is not None:
                    evidence["externalPatientId"] = str(chart)
                    active = self.state.patient.active
                    evidence["superseded"] = active is None or active.patientId != chart
                self.state.reporter.record("patient", evidence, call_id=call_id)
            return self._record(key, outcome, answer)

        return await self._run_write(
            lambda: self._resolver.registering(r.firstName, r.dob), create
        )

    def _created(
        self, result: CreationReceipt, r: Registration, plan: PlanSummary
    ) -> tuple[WriteOutcome, dict]:
        expected = {full_name(r), exact_name(f"{r.lastName} {r.firstName}")}
        if exact_name(result.name) not in expected or not dob_matches(
            result.dob, r.dob
        ):
            return "uncertain", staff("invalid_receipt")
        patient = Receipt(
            status="verified",
            patientId=result.patientId,
            name=result.name,
            dob=result.dob,
            phone=r.phone or self.state.call.caller_phone,
            insuranceCarrier=plan.label if result.status == "created" else None,
            appointmentsStatus="none",
            appointments=[],
        )
        self.state.insurance.registrations[result.patientId] = result.status
        activated = self._resolver.activate_created(r.firstName, r.dob, patient)
        status = "success" if result.status == "created" and activated else "blocked"
        if result.status == "created":
            coverage = "self-pay recorded" if plan.selfPay else "insurance attached"
            answer = f"{status}: New patient chart created with {coverage}."
        else:
            answer = f"{status}: Created the patient chart for {result.name}."
        if result.status == "partial":
            answer += " Insurance attachment is not confirmed. Office staff must finish registration; do not create another chart."
        if not activated:
            answer += f" The chart is for {result.name}. The patient context changed while this was running; this receipt was not applied to the current patient. Do not repeat chart creation."
        return "written", reply(result.status, answer)

    async def update(
        self,
        plan_id: str,
        coverage_type: CoverageType,
        member_id: str,
        *,
        call_id: str | None = None,
    ) -> dict:
        active = self.state.patient.active
        if active is None:
            return reply(
                "needs_resolution",
                "needs_input: Verify the patient before changing insurance.",
            )
        plan = listed_plan(self.state, plan_id, coverage_type)
        if isinstance(plan, dict):
            return plan
        member_id = "self pay" if plan.selfPay else member_id.strip()
        if not member_id:
            return reply(
                "needs_member_id",
                "needs_input: What is the member ID on the insurance card?",
            )
        office = self.state.call.called_office_key
        subject = (office, active.patientId)
        key = WriteKey(
            subject, "update_insurance", (plan.planId, coverage_type, member_id)
        )
        if blocked := self._ledger.blocking(subject):
            return blocked
        if (saved := self._ledger.get(key)) and self._ledger.latest(subject) == key:
            return saved
        revision = self.state.patient.revision

        async def update():
            result = await self._middleware.update(
                InsuranceChange(
                    patientId=active.patientId,
                    dob=active.dob,
                    coverageType=coverage_type,
                    subscriberNum=member_id,
                    insurancePlanId=plan.planId,
                    office=office,
                )
            )
            outcome: WriteOutcome
            if isinstance(result, WriteFailure):
                outcome = result.status
                if result.status == "failed":
                    answer = reply(
                        "failed",
                        f"needs_input: Insurance was not changed ({result.reason}). Resolve the failure before trying again.",
                    )
                elif result.status == "partial":
                    answer = reply(
                        "partial",
                        "blocked: The previous insurance plan was ended, but the replacement was not attached. Office staff must finish the change. Do not repeat this write.",
                    )
                else:
                    answer = staff(result.status)
            elif result.patientId != active.patientId:
                outcome, answer = "uncertain", staff("uncertain")
            else:
                outcome = "written"
                if active.patientId in self.state.insurance.registrations:
                    self.state.insurance.registrations[active.patientId] = "created"
                if self._resolver.refresh_insurance(
                    active, result.newInsurance, revision
                ):
                    answer = reply(
                        "updated", f"success: Updated insurance to {plan.label}."
                    )
                else:
                    answer = reply(
                        "updated",
                        f"blocked: Updated insurance for {active.name}. Patient context changed; the receipt was not applied to the current patient. Do not repeat this write.",
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
            return self._record(key, outcome, answer)

        return await self._run_write(
            lambda: (
                self.state.patient.active is active
                and self.state.patient.revision == revision
            ),
            update,
        )
