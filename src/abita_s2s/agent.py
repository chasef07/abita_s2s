"""Define the voice persona, greeting, and model-facing tools."""

import json
import logging
from typing import Literal

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolFlag

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration, Registration, staff
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.prompt import load_prompt
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(
        self,
        office: OfficeProfile,
        knowledge: OfficeKnowledge,
        resolver: PatientResolver | None = None,
        insurance: InsuranceRegistration | None = None,
    ) -> None:
        super().__init__(
            instructions=(
                load_prompt("speaker")
                + f"\n\nCurrent office: {office.display_name} ({office.key})."
            )
        )
        self._greeting = office.greeting
        self._knowledge = knowledge
        self._resolver = resolver
        self._insurance = insurance

    @function_tool(flags=ToolFlag.CANCELLABLE)
    async def resolve_patient(
        self, context: RunContext[CallState], firstName: str | None, dob: str | None
    ) -> str:
        """Call immediately with the supplied patient's firstName and dob:null if unknown.

        Include a supplied DOB without separate confirmation and follow the returned next step.
        Same-name patient switches require DOB. If unresolved, clarify first-name spelling
        and DOB before offering staff help. Use only caller-provided identity.

        Args:
            firstName: First name of the patient receiving care; null if unknown.
            dob: Patient date of birth in MM/DD/YYYY; null if unknown.
        """
        if self._resolver is None or self._resolver.state is not context.userdata:
            return json.dumps(
                {
                    "outcome": "lookup_failed",
                    "answer": "Patient lookup is unavailable. Ask office staff for help.",
                    "next_input": "staff_help",
                }
            )
        return json.dumps(
            await self._resolver.resolve(firstName, dob), ensure_ascii=False
        )

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
        insuranceMemberId: str, ssnLast4: str | None,
        newPatientConfirmed: Literal[True] | None, readBack: Literal[True] | None,
    ) -> str:
        """Create a chart after complete resolution, accepted coverage and confirmation.

        Confirm first registration, callback number, and the full identity, contact,
        address and insurance read-back before setting confirmation flags true.
        Use the patient's details, not the caller's. DOB uses MM/DD/YYYY.
        Pass phone:null only when the inbound callback number was confirmed.
        Request SSN last four once for insured routine vision; use null if unavailable
        or declined, and skip for self-pay. Never repeat SSN in read-back.
        Claim success only from this receipt; never retry full or partial creation.
        """
        if self._insurance is None or self._insurance.state is not context.userdata:
            return json.dumps(staff())
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
            ssnLast4=ssnLast4,
            newPatientConfirmed=newPatientConfirmed,
            readBack=readBack,
        )
        return json.dumps(await self._insurance.add(registration))

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
        return json.dumps(await self._insurance.update(insuranceMemberId))

    @function_tool
    async def search_office_knowledge(
        self, context: RunContext[CallState], query: str
    ) -> str:
        """Search this office's providers, hours, location, and practice policies.

        Args:
            query: A short non-patient office question. Omit
                patient names, identifiers, and personal medical details.
        """
        result = await self._knowledge.search(
            context.userdata.call.called_office_key, query
        )
        return json.dumps(result, ensure_ascii=False)

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=f'Greet the caller: "{self._greeting}" Then listen.'
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
