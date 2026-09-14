"""Define the voice persona and initial greeting."""

import logging

from livekit.agents import Agent

from abita_s2s.prompt import load_prompt
from abita_s2s.offices import OfficeProfile

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(self, office: OfficeProfile) -> None:
        super().__init__(instructions=(
            load_prompt("speaker")
            + f"\n\nCurrent office: {office.display_name} ({office.key})."
        ))
        self._greeting = office.greeting

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=f'Greet the caller: "{self._greeting}" Then listen.'
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
