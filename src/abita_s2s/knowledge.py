"""Read the called office's whole knowledge base once per call, as text for the thinker's prompt."""

import logging
import re
from collections.abc import Iterable

from abita_s2s.integrations.product import KnowledgeEntry, Product

logger = logging.getLogger(__name__)

UNAVAILABLE = "Office information could not be loaded for this call."

_STATUS = re.compile(
    r"^Status:\s*(?:available|not-supplied|not-offered)\s*(?:\r?\n|$)", re.IGNORECASE
)


def render_office_knowledge(entries: Iterable[KnowledgeEntry]) -> str:
    """Each entry's title and text in order, without Product's status labels."""
    return "\n\n".join(
        f"{entry.title}\n{_STATUS.sub('', entry.text).strip()}".strip()
        for entry in entries
    )


async def read_office_knowledge(product: Product, office_key: str) -> str:
    """The office's knowledge as prompt text, or the unavailable notice with its cause logged."""
    revision = await product.read_knowledge(office_key)
    if isinstance(revision, str):
        logger.error(
            "Office knowledge unavailable office=%s cause=%s", office_key, revision
        )
        return UNAVAILABLE
    logger.info(
        "Office knowledge loaded office=%s revision=%s entries=%d",
        office_key,
        revision.revisionId,
        len(revision.entries),
    )
    return render_office_knowledge(revision.entries)
