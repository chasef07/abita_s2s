"""Define the voice persona, greeting, and model-facing tools."""

import json
import logging

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolFlag

from abita_s2s.identity import PatientResolver
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.prompt import load_prompt
from abita_s2s.staff_tasks import Category, StaffTasks, Urgency
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(
        self,
        office: OfficeProfile,
        knowledge: OfficeKnowledge,
        resolver: PatientResolver | None = None,
        staff_tasks: StaffTasks | None = None,
    ) -> None:
        super().__init__(
            tools=[function_tool(self.create_staff_task)]
            if office.staff_tasks_enabled
            else [],
            instructions=(
                load_prompt("speaker")
                + f"\n\nCurrent office: {office.display_name} ({office.key})."
            ),
        )
        self._greeting = office.greeting
        self._knowledge = knowledge
        self._resolver = resolver
        self._staff_tasks = staff_tasks

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
            await self._staff_tasks.submit(category, urgency, summary, message)
        )

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=f'Greet the caller: "{self._greeting}" Then listen.'
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
