"""Build the voice agent from the registry's tools for one call and seed its greeting; startup owns lifecycle."""

import logging
from datetime import datetime

from livekit.agents import Agent, llm

from abita_s2s.offices import EASTERN
from abita_s2s.prompt import greeting, load_prompt
from abita_s2s.tools.registry import Owners, offer

logger = logging.getLogger(__name__)


class AbitaAgent(Agent):
    def __init__(self, owners: Owners) -> None:
        self._practice_name = owners.office.greeting_name
        self._greeting = greeting(owners.office.greeting_name, datetime.now(EASTERN))
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="developer", content=self._greeting)
        super().__init__(
            tools=offer(owners),
            instructions=load_prompt("speaker"),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        handle = self.session.generate_reply(instructions=self._greeting)
        await handle
        if handle.exception() is not None:
            logger.warning("GPT-Live did not complete the initial greeting")
