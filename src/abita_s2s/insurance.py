"""Per-call participation, registration and insurance-write owner."""

import asyncio
import re
from dataclasses import replace
from typing import Literal

from pydantic import ConfigDict

from abita_s2s.identity import PatientResolver, reply
from abita_s2s.insurance_rules import match_plan, normalize
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
        self._creations: list[tuple[tuple, CreationReceipt | WriteFailure, dict]] = []
        self._updates: list[tuple[tuple, UpdatedReceipt | WriteFailure, dict]] = []
        self._task: asyncio.Task | None = None
        self._closed = False

    def close_admission(self) -> None:
        self._closed = True

    async def aclose(self):
        self._closed = True
        if self._task:
            await asyncio.shield(self._task)

    def check(self, plan: str, coverage_type: CoverageType) -> dict:
        self.state.insurance.accepted = None
        if self.state.patient.active:
            self.state.insurance.checked_patients.add(
                (self.state.call.called_office_key, self.state.patient.active.patientId)
            )
        result = match_plan(
            self.state.call.called_office_key, plan.strip(), coverage_type
        )
        canonical = result.pop("canonical_plan")
        if canonical:
            patient = self.state.patient
            self.state.insurance.accepted = AcceptedInsurance(
                self.state.call.called_office_key,
                patient.revision,
                patient.active.patientId if patient.active else None,
                patient.absence if not patient.active else None,
                canonical,
                coverage_type,
            )
        return result

    async def _run_write(self, work):
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
                return await work()
            except BaseException:
                self.state.insurance.write_uncertain = True
                raise
            finally:
                self.state.insurance.write_pending = False

        self._task = asyncio.create_task(run())
        return await asyncio.shield(self._task)

    async def add(self, registration: Registration, *, call_id: str | None = None) -> dict:
        r = registration
        key = (
            self.state.call.called_office_key,
            exact_name(r.firstName),
            parse_dob(r.dob),
        )
        for prior, _, result in self._creations:
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
        self_pay = normalize(checked.plan) == "self pay"
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
            return reply("needs_callback", "needs_input: Ask for a valid callback phone number.")
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
                else f"Coverage is {checked.plan}, policyholder {r.subscriberName}, member ID {r.insuranceMemberId}."
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
            insurance=checked.plan,
            subscriberName=r.subscriberName or f"{r.firstName} {r.lastName}",
            subscriberNum="self pay" if self_pay else r.insuranceMemberId,
        )
        if r.email:
            payload["email"] = r.email
        if checked.coverage_type == "routine_vision":
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
                    evidence["superseded"] = active is None or active.patientId != result.patientId
                self.state.reporter.record("patient", evidence, call_id=call_id)
            self._creations.append((key, result, answer))
            return answer

        return await self._run_write(create)

    def _created(
        self, result: CreationReceipt, r: Registration, checked
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
            insuranceCarrier=checked.plan if result.status == "created" else None,
            routing=result.routing,
            allowedProviders=result.allowedProviders,
            preauthRequired=result.preauthRequired,
            appointmentsStatus="none",
            appointments=[],
        )
        self.state.insurance.registrations[result.patientId] = result.status
        activated = (
            accepted_insurance(self.state) is checked
            and self._resolver.activate_created(checked.patient_revision, patient)
        )
        if activated and self.state.insurance.accepted is checked:
            self.state.insurance.accepted = replace(
                checked,
                patient_revision=self.state.patient.revision,
                patient_id=result.patientId,
                absence=None,
            )
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
        member_id = (
            "self pay" if normalize(checked.plan) == "self pay" else member_id.strip()
        )
        if not member_id:
            return reply(
                "needs_member_id", "What is the member ID on the insurance card?"
            )
        key = (
            checked.office_key,
            active.patientId,
            checked.plan,
            checked.coverage_type,
            member_id,
        )
        for prior, _, result in reversed(self._updates):
            if prior[:2] == key[:2]:
                if prior == key and normalize(
                    active.insuranceCarrier or ""
                ) == normalize(checked.plan):
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
                "insurance": checked.plan,
                "coverageType": checked.coverage_type,
                "subscriberNum": member_id,
            }
            result = await self._middleware.update(checked.office_key, payload)
            if (
                not isinstance(result, UpdatedReceipt)
                or result.patientId != active.patientId
                or normalize(result.newInsurance) != normalize(checked.plan)
            ):
                self.state.insurance.write_uncertain = True
                answer = staff("uncertain")
            else:
                if active.patientId in self.state.insurance.registrations:
                    self.state.insurance.registrations[active.patientId] = "created"
                updated = active.model_copy(
                    update={
                        "insuranceCarrier": result.newInsurance,
                        "insuranceCarrierId": None,
                        "insPlanId": None,
                        "respPartyId": references.respPartyId,
                        "routing": result.routing,
                        "allowedProviders": result.allowedProviders,
                        "routingAmbiguous": result.routingAmbiguous,
                        "preauthRequired": result.preauthRequired,
                    }
                )
                if (
                    self.state.patient.revision == checked.patient_revision
                    and self._resolver.refresh_insurance(active, updated)
                ):
                    if self.state.insurance.accepted is checked:
                        self.state.insurance.accepted = replace(
                            checked, patient_revision=self.state.patient.revision
                        )
                    answer = reply(
                        "updated", f"Updated insurance to {result.newInsurance}."
                    )
                else:
                    answer = reply(
                        "updated",
                        f"Updated insurance for {active.name}. Patient context changed; the receipt was not applied to the current patient.",
                    )
            if self.state.reporter:
                self.state.reporter.record("insurance", {
                    "outcome": answer["outcome"], "externalPatientId": str(active.patientId),
                }, call_id=call_id)
            self._updates.append((key, result, answer))
            return answer

        return await self._run_write(update)
