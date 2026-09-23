"""Model-facing patient tools."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration, staff
from abita_s2s.scheduling import Scheduling
from abita_s2s.state import CallState


class PatientTools:
    def __init__(
        self,
        resolver: PatientResolver | None,
        insurance: InsuranceRegistration | None,
        scheduling: Scheduling | None,
    ) -> None:
        self._resolver = resolver
        self._insurance = insurance
        self._scheduling = scheduling

    @function_tool
    async def resolve_patient(
        self, context: RunContext[CallState], firstName: str | None, dob: str | None
    ) -> str:
        """Resolve existing patients only; do not call for callers who say they are new.

        Call with the patient's firstName and dob:null unless DOB was already
        provided. The tool waits for any pending phone lookup, matches private
        phone profiles first, and asks for DOB if needed.

        Include a supplied DOB without separate confirmation and follow the returned next step.
        Same-name patient switches require DOB. If unresolved, clarify first-name spelling
        and DOB before offering staff help. Use only caller-provided identity.

        Args:
            firstName: First name of the patient receiving care; null if unknown.
            dob: Patient date of birth in MM/DD/YYYY; null if unknown.
        """
        if self._resolver is None or self._resolver.state is not context.userdata:
            return "blocked: Patient lookup is unavailable. Ask office staff for help."
        result = await self._resolver.resolve(
            firstName, dob, call_id=context.function_call.call_id
        )
        appointments = self._scheduling.appointments_text() if self._scheduling else ""
        return result["answer"] + appointments

    @function_tool
    async def add_patient(
        self,
        context: RunContext[CallState],
        firstName: str,
        lastName: str,
        dob: str,
        phone: str | None,
        inboundPhoneConfirmed: Literal[True] | None,
        email: str | None,
        street: str,
        aptSuite: str | None,
        city: str,
        state: str,
        zip: str,
        sex: Literal["male", "female"],
        subscriberName: str,
        insuranceMemberId: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Create a new patient chart and attach insurance from completed intake.

        Requires accepted insurance for the visit type and caller confirmation of
        the final read-back. No existing-chart lookup is required.
        Returns a plain-text status and result. A blocked result may mean the
        chart exists but insurance is not confirmed. Do not repeat chart creation
        after successful creation, partial creation, or an uncertain result.

        Args:
            firstName: Patient's first name.
            lastName: Patient's last name.
            dob: Patient's date of birth in MM/DD/YYYY.
            phone: Patient's callback number; null only when the inbound number was confirmed.
            inboundPhoneConfirmed: True if the caller approved the inbound number for the file; otherwise null.
            email: Patient's email address, or null if unavailable or declined.
            street: Street number and street name.
            aptSuite: Apartment, unit, or suite; null if none.
            city: City of the patient's address.
            state: Two-letter state abbreviation.
            zip: ZIP code of the patient's address.
            sex: Patient's sex for registration.
            subscriberName: Name on the insurance card; reuse the patient's name if confirmed as theirs.
            insuranceMemberId: Member ID on the insurance card; use "self pay" for self-pay.
            readBack: True only after the caller confirms the complete final read-back; otherwise null.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
            return staff()["answer"]
        registration = Registration(
            firstName=firstName,
            lastName=lastName,
            dob=dob,
            phone=phone,
            inboundPhoneConfirmed=inboundPhoneConfirmed,
            email=email,
            street=street,
            aptSuite=aptSuite,
            city=city,
            state=state,
            zip=zip,
            sex=sex,
            subscriberName=subscriberName,
            insuranceMemberId=insuranceMemberId,
            readBack=readBack,
        )
        result = await self._insurance.add(
            registration, call_id=context.function_call.call_id
        )
        return result["answer"]
