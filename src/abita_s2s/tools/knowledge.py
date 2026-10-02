"""Model-facing knowledge tools."""

from livekit.agents import RunContext, function_tool

from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.state import CallState


class KnowledgeTools:
    def __init__(self, knowledge: OfficeKnowledge) -> None:
        self._knowledge = knowledge

    @function_tool
    async def search_office_knowledge(
        self, context: RunContext[CallState], query: str
    ) -> str:
        """Look up this office's providers, hours, locations, services, and policies.

        Returns office information, not patient records or live appointment availability.
        Use check_insurance for plan acceptance.

        Args:
            query: One office topic as a short English question. Omit the
                office name, addresses, and patient details.
        """
        result = await self._knowledge.search(
            context.userdata.call.called_office_key, query
        )
        return result["answer"]
