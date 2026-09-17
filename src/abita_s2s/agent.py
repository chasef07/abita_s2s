"""Define the voice persona, greeting, and model-facing tools."""

import json
import logging
from typing import Literal

from livekit.agents import Agent, RunContext, function_tool

from abita_s2s.call_control import CallControl
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration, staff
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.prompt import load_prompt
from abita_s2s.scheduling import Scheduling
from abita_s2s.staff_tasks import Category, StaffTasks, Urgency
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(
        self,
        office: OfficeProfile,
        knowledge: OfficeKnowledge,
        resolver: PatientResolver | None = None,
        insurance: InsuranceRegistration | None = None,
        scheduling: Scheduling | None = None,
        staff_tasks: StaffTasks | None = None,
        call_control: CallControl | None = None,
    ) -> None:
        tools = list(scheduling.tools) if scheduling else []
        if office.staff_tasks_enabled:
            tools.append(function_tool(self.create_staff_task))
        if call_control:
            tools.append(call_control)
        super().__init__(tools=tools,
            instructions=(
                load_prompt("speaker")
                + f"\n\nCurrent office: {office.display_name} ({office.key})."
            ),
        )
        self._greeting = office.greeting
        self._knowledge = knowledge
        self._resolver = resolver
        self._insurance = insurance
        self._scheduling = scheduling
        self._staff_tasks = staff_tasks

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
        result = await self._resolver.resolve(firstName, dob, call_id=context.function_call.call_id)
        appointments = self._scheduling.appointments_text() if self._scheduling else ""
        return result["answer"] + appointments

    @function_tool
    async def check_insurance(
        self, context: RunContext[CallState], plan: str,
        coverageType: Literal["medical", "routine_vision"],
    ) -> str:
        """Check office participation for the caller's plan and triaged visit type.

        Use before registration or a requested insurance change. Follow clarification
        or staff-review instructions; acceptance does not establish active benefits.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
            return json.dumps(staff())
        return json.dumps(self._insurance.check(plan, coverageType))

    @function_tool
    async def add_patient(
        self, context: RunContext[CallState], firstName: str, lastName: str, dob: str,
        phone: str | None, inboundPhoneConfirmed: Literal[True] | None,
        email: str | None, street: str, aptSuite: str | None, city: str, state: str,
        zip: str, sex: Literal["male", "female"], subscriberName: str,
        insuranceMemberId: str, readBack: Literal[True] | None,
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
        result = await self._insurance.add(registration, call_id=context.function_call.call_id)
        return result["answer"]

    @function_tool
    async def update_insurance(
        self, context: RunContext[CallState], insuranceMemberId: str,
    ) -> str:
        """Change the active verified patient's coverage only when the caller requests it.

        First use check_insurance for the new plan and correct visit type. Supply the
        card member ID, or self pay after Self Pay is accepted. Use add_patient for
        registration. Claim success only from an updated receipt; never retry an
        uncertain result or repeat a completed write.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
            return json.dumps(staff())
        return json.dumps(
            await self._insurance.update(insuranceMemberId, call_id=context.function_call.call_id)
        )

    @function_tool
    async def search_office_knowledge(
        self, context: RunContext[CallState], query: str
    ) -> str:
        """Look up this office's providers, hours, locations, services, and policies.

        Returns office information, not patient records or live appointment availability.
        Use check_insurance for plan acceptance.

        Args:
            query: A focused office question. Omit patient names, identifiers,
                and personal medical details.
        """
        result = await self._knowledge.search(
            context.userdata.call.called_office_key, query
        )
        return result["answer"]

    async def create_staff_task(
        self,
        context: RunContext[CallState],
        category: Category,
        urgency: Urgency,
        summary: str,
        message: str,
    ) -> str:
        """Send one safe, non-urgent caller-approved unresolved need per invocation.

        Submit distinct needs separately, even in one category. For records, search
        office knowledge for intake/delivery rules; speak restrictions and missing
        prerequisites even when approved. Collect details and list gaps if incomplete.
        Follow Human Transfer policy for urgent or clinical concerns. Confirm submission
        only after success; staff owns fulfillment and timing.

        Args:
            category: Optical includes glasses/contact prescriptions; medication includes
                refills and medication authorizations; insurance includes copays, coverage,
                referrals requirements and service authorizations; referrals means specialist
                or imaging orders. pre_op/post_op are surgical preparation/aftercare.
                Ask what prior authorization authorizes; if still unclear use other.
            urgency: high_priority for time-sensitive non-clinical work, normal for
                standard follow-up, non_urgent without time sensitivity. Transfer clinical acuity.
            summary: Short staff inbox title for one unresolved need.
            message: Details of exactly one need. Include medication/pharmacy, service/plan,
                authorization status and procedure timing when relevant. Preserve intake
                and list missing details. Make another invocation for each additional need.
        """
        context.disallow_interruptions()
        if self._staff_tasks is None or self._staff_tasks.state is not context.userdata:
            return json.dumps(
                {
                    "outcome": "failed",
                    "answer": "Staff delivery is unavailable. No request was sent.",
                }
            )
        return json.dumps(
            await self._staff_tasks.submit(
                category, urgency, summary, message, call_id=context.function_call.call_id
            )
        )

    async def on_exit(self) -> None:
        if self._resolver is not None:
            await self._resolver.aclose()

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=f'Greet the caller: "{self._greeting}" Then listen.'
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
