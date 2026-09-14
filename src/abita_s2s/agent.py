"""Define the voice persona, greeting, and model-facing tools."""

import json
import logging

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolFlag

from abita_s2s.identity import PatientResolver
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

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=f'Greet the caller: "{self._greeting}" Then listen.'
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
