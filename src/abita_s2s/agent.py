"""Define the voice persona, greeting, and model-facing tools."""

import json
import logging

from livekit.agents import Agent, RunContext, function_tool

from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.offices import OfficeProfile
from abita_s2s.prompt import load_prompt
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(self, office: OfficeProfile, knowledge: OfficeKnowledge) -> None:
        super().__init__(
            instructions=(
                load_prompt("speaker")
                + f"\n\nCurrent office: {office.display_name} ({office.key})."
            )
        )
        self._greeting = office.greeting
        self._knowledge = knowledge

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
