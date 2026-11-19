"""Model-facing patient tools."""

from typing import Literal

from livekit.agents import RunContext, function_tool

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration
from abita_s2s.insurance_contract import CoverageType
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.state import CallState


class PatientTools:
    def __init__(
        self,
        resolver: PatientResolver,
        insurance: InsuranceRegistration,
        scheduling: Scheduling,
    ) -> None:
        self._resolver = resolver
        self._insurance = insurance
        self._scheduling = scheduling

    @function_tool
    async def resolve_patient(
        self, context: RunContext[CallState], firstName: str | None, dob: str | None
    ) -> str:
        """Find an existing patient; never call for a caller who says they are new.

        Use only caller-provided identity, and follow the returned next step; the
        tool asks for DOB when needed, and switching to a same-name patient requires
        it. If none matches, clarify the first-name spelling and DOB, then start new
        patient intake.

        Args:
            firstName: First name of the patient receiving care; null if unknown.
            dob: Patient date of birth in MM/DD/YYYY if the caller already gave it,
                without confirming it separately; otherwise null.
        """
        result = await self._resolver.resolve(
            firstName, dob, call_id=context.function_call.call_id
        )
        if result["outcome"] in ("verified", "switched"):
            return result["answer"] + self._scheduling.appointments_text()
        return result["answer"]

    @function_tool
    async def add_patient(
        self,
        context: RunContext[CallState],
        firstName: str,
        lastName: str,
        dob: str,
        callback: str,
        email: str | None,
        street: str,
        aptSuite: str | None,
        city: str,
        state: str,
        zip: str,
        sex: Literal["male", "female"],
        visitType: CoverageType,
        insurancePlanId: str,
        subscriberName: str,
        insuranceMemberId: str,
        readBack: Literal[True] | None,
    ) -> str:
        """Create a new patient chart and attach insurance after completed intake; no chart lookup is needed.

        A blocked result may mean the chart exists but insurance is not confirmed;
        never create it again after a created, partial, or uncertain result.

        Args:
            dob: Patient's date of birth in MM/DD/YYYY.
            callback: The literal "inbound" if the caller approved the number they called from; otherwise the number they gave.
            email: Patient's email address, or null if unavailable or declined.
            street: Street number and street name.
            aptSuite: Apartment, unit, or suite; null if none.
            state: Two-letter state abbreviation.
            visitType: The triaged visit type.
            insurancePlanId: The patient's plan ID from the Insurance list for visitType.
            subscriberName: Name on the insurance card; reuse the patient's name if confirmed as theirs.
            insuranceMemberId: Member ID on the insurance card; empty for a self-pay plan.
            readBack: True only after the caller confirms the complete final read-back; otherwise null.
        """
        registration = Registration(
            firstName=firstName,
            lastName=lastName,
            dob=dob,
            callback=callback,
            email=email,
            street=street,
            aptSuite=aptSuite,
            city=city,
            state=state,
            zip=zip,
            sex=sex,
            visitType=visitType,
            insurancePlanId=insurancePlanId,
            subscriberName=subscriberName,
            insuranceMemberId=insuranceMemberId,
            readBack=readBack,
        )
        result = await self._insurance.add(
            registration, call_id=context.function_call.call_id
        )
        return result["answer"]
