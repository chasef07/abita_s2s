"""Per-call participation, registration and insurance-write owner."""

import asyncio
import re
from typing import Literal

from pydantic import ConfigDict

from abita_s2s.identity import PatientResolver, reply
from abita_s2s.insurance_state import (
    AcceptedInsurance,
    CoverageType,
    accepted_insurance,
)
from abita_s2s.middleware import Receipt, Record
from abita_s2s.name_matcher import dob_matches, exact_name, parse_dob
from abita_s2s.registration_middleware import (
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
        if self._task:
            await asyncio.shield(self._task)

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

    async def _run_write(self, checked: AcceptedInsurance, work):
        if self._closed:
            return staff()
        if self._task and not self._task.done():
            return reply(
                "write_pending",
                "blocked: A registration or insurance write is still in progress. Wait for its result.",
            )
        if self.state.insurance.write_uncertain:
            return staff("uncertain")
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
        if self.state.patient.active:
            return reply(
                "already_active",
                "blocked: A verified patient is already active. Resolve the intended patient before creating a chart.",
            )
        checked = accepted_insurance(self.state)
        if checked is None:
            return reply(
                "needs_insurance",
                "needs_input: Check accepted coverage for this patient and the intended medical or routine vision visit before registration.",
            )
        if checked.decision.participation != "accepted":
            return reply(checked.decision.outcome, checked.decision.answer)
        self_pay = checked.decision.selfPay
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
                answer = staff(result.status)
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
            insuranceDecision=result.insuranceDecision,
            appointmentsStatus="none",
            appointments=[],
        )
        self.state.insurance.registrations[result.patientId] = result.status
        activated = self._resolver.activate_created(checked, patient)
        status = "success" if result.status == "created" and activated else "blocked"
        answer = f"{status}: Created the patient chart for {result.name}."
        if result.status == "partial":
            answer += " Insurance attachment is not confirmed. Office staff must finish registration; do not create another chart."
        if not activated:
            answer += " The patient context changed while this was running; this receipt was not applied to the current patient. Do not repeat chart creation."
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
                if prior == key and normalize(
                    active.insuranceCarrier or ""
                ) == normalize(checked.decision.canonicalPlan):
                    return result
                break

        async def update():
            references = active
            if not references.insPlanId or not references.respPartyId:
                references = await self._resolver.read_insurance(active)
                if (
                    references is None
                    or not references.insPlanId
                    or not references.respPartyId
                    or (
                        active.insuranceCarrier is not None
                        and normalize(references.insuranceCarrier or "")
                        != normalize(active.insuranceCarrier)
                    )
                    or accepted_insurance(self.state) is not checked
                ):
                    return reply(
                        "needs_staff_review",
                        "Current insurance details could not be verified. No insurance change was sent; ask office staff for help.",
                    )
            payload = {
                "patientId": active.patientId,
                "dob": active.dob,
                "insPlanId": references.insPlanId,
                "respPartyId": references.respPartyId,
                "oldInsurance": references.insuranceCarrier or "",
                "insurance": checked.decision.canonicalPlan,
                "coverageType": checked.decision.coverageType,
                "subscriberNum": member_id,
            }
            result = await self._middleware.update(checked.office_key, payload)
            if (
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
                        "insPlanId": None,
                        "respPartyId": references.respPartyId,
                        "insuranceDecision": result.insuranceDecision,
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
            self._updates.append((key, answer))
            return answer

        return await self._run_write(checked, update)
