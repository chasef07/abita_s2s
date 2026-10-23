"""Search Product's current office corpus; expose facts, never a local fallback."""

import logging
import re

from abita_s2s.integrations.product import Product
from abita_s2s.results import reply

logger = logging.getLogger(__name__)

_STATUS = re.compile(
    r"^Status:\s*(?:available|not-supplied|not-offered)\s*(?:\r?\n|$)", re.IGNORECASE
)


class OfficeKnowledge:
    def __init__(self, product: Product) -> None:
        self._product = product

    async def search(self, office_key: str, query: str) -> dict[str, str]:
        query = query.strip()
        if not 3 <= len(query) <= 500:
            return reply(
                "invalid_query",
                "needs_input: Provide a focused office question between 3 and 500 characters.",
            )
        result = {"office": office_key, "query": query}
        data = await self._product.search(office_key, query)
        if isinstance(data, str):
            cause = data
        elif data.outcome == "temporary_failure":
            cause = data.outcome
        elif data.outcome == "no_relevant_information":
            return reply(
                data.outcome,
                "no_results: No information was found for this question. "
                "This does not establish that the service is unavailable.",
                **result,
            )
        else:
            answer = "\n".join(
                _STATUS.sub("", p.text).strip() for p in data.passages
            ).strip()
            if answer:
                return reply("found", f"success: {answer}", **result)
            cause = "empty_answer"
        logger.warning(
            "Office knowledge unavailable office=%s cause=%s", office_key, cause
        )
        return reply(
            "temporary_failure",
            "blocked: Office information could not be checked. "
            "Offer staff help if needed.",
            **result,
        )
