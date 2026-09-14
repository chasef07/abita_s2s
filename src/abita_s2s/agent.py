"""Define the voice persona and initial greeting."""

import logging

from livekit.agents import Agent

from abita_s2s.prompt import load_prompt

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=load_prompt("speaker"))

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(
            instructions=(
                'Greet the caller: "Thank you for calling Abita Eye Group. '
                'How can I help you today?" Then listen.'
            )
        )
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
