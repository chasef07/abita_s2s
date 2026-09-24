"""Assemble the voice agent and own its greeting and lifecycle."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import Agent

from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.prompt import load_prompt
from abita_s2s.scheduling import Scheduling
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.tools.call_control import CallControl
from abita_s2s.tools.insurance import InsuranceTools
from abita_s2s.tools.knowledge import KnowledgeTools
from abita_s2s.tools.patients import PatientTools
from abita_s2s.tools.scheduling import SchedulingTools
from abita_s2s.tools.staff_tasks import StaffTaskTools

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
        patients = PatientTools(resolver, insurance, scheduling)
        coverage = InsuranceTools(insurance)
        office_knowledge = KnowledgeTools(knowledge)
        tools = list(SchedulingTools(scheduling).tools) if scheduling else []
        if office.staff_tasks_enabled:
            tools.append(StaffTaskTools(staff_tasks).create_staff_task)
        if call_control:
            tools.append(call_control)
        # Preserve the original tool order, including Agent's former method discovery.
        tools.extend(
            [
                patients.add_patient,
                coverage.check_insurance,
                coverage.check_new_patient_eligibility,
                patients.resolve_patient,
                office_knowledge.search_office_knowledge,
                coverage.update_insurance,
            ]
        )
        super().__init__(
            tools=tools,
            instructions=(
                load_prompt("speaker")
                + f"\n\nCurrent office: {office.display_name} ({office.key})."
            ),
        )
        self._practice_name = office.greeting_name
        self._resolver = resolver

    async def on_exit(self) -> None:
        if self._resolver is not None:
            await self._resolver.aclose()

    async def on_enter(self) -> None:
        now = datetime.now(ZoneInfo("America/New_York"))
        handle = self.session.generate_reply(
            instructions=(
                "Greet the caller with “Good morning,” “Good afternoon,” or “Good evening,” "
                "using office-local time. Name the practice, introduce yourself as Sofia, "
                "and ask how you can help. Be warm, caring, and upbeat; vary wording "
                "slightly, keep it brief, then listen. "
                f"Office-local time: {now:%H:%M %Z} (America/New_York). "
                f'Practice name: "{self._practice_name}"'
            )
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
