"""Search Product's current office corpus; expose facts, never a local fallback."""

import asyncio
import logging
import re
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from abita_s2s.config import Config

logger = logging.getLogger(__name__)
SEARCH_TIMEOUT = 4.0

_STATUS = re.compile(
    r"^Status:\s*(?:available|not-supplied|not-offered)\s*(?:\r?\n|$)", re.IGNORECASE
)
_Id = Annotated[str, Field(min_length=1, max_length=100)]


class _Passage(BaseModel):
    model_config = ConfigDict(strict=True)
    revisionId: _Id
    sectionId: _Id
    title: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=12000)]


class _SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    outcome: Literal["found", "no_relevant_information", "temporary_failure"]
    revisionId: _Id | None = None
    passages: Annotated[list[_Passage], Field(max_length=8)]

    @model_validator(mode="after")
    def coherent_revision(self) -> Self:
        if self.outcome == "found":
            if (
                not self.revisionId
                or not self.passages
                or any(p.revisionId != self.revisionId for p in self.passages)
            ):
                raise ValueError("Incomplete or mixed corpus revision")
        elif self.passages:
            raise ValueError("Unexpected passages for unsuccessful search")
        return self


class OfficeKnowledge:
    def __init__(self, client: httpx.AsyncClient, config: Config) -> None:
        self._client = client
        self._url = config.knowledge_url
        self._secret = config.product_secret

    async def search(self, office_key: str, query: str) -> dict[str, str]:
        query = query.strip()
        if not 3 <= len(query) <= 500:
            return {
                "outcome": "invalid_query",
                "answer": "Use an office question between 3 and 500 characters.",
            }
        # Keep each result tied to its question when overlapping reads finish out of order.
        result = {"office": office_key, "query": query}
        try:
            if not self._url or not self._secret:
                raise ValueError("Knowledge is not configured")
            async with asyncio.timeout(SEARCH_TIMEOUT):
                response = await self._client.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._secret}",
                        "X-Office-Key": office_key,
                    },
                    json={"query": query},
                    timeout=SEARCH_TIMEOUT,
                    follow_redirects=False,
                )
                response.raise_for_status()
                data = _SearchResponse.model_validate(response.json())
            if data.outcome == "temporary_failure":
                raise ValueError("Knowledge backend unavailable")
            if data.outcome == "no_relevant_information":
                return {
                    **result,
                    "outcome": data.outcome,
                    "answer": "No relevant office information was found for this question.",
                }
            answer = "\n".join(
                _STATUS.sub("", p.text).strip() for p in data.passages
            ).strip()
            if not answer:
                raise ValueError("Empty office answer")
            return {**result, "outcome": "found", "answer": answer}
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            # Never log queries, credentials, response bodies, or validation input.
            cause = (
                f"http_{exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            logger.warning(
                "Office knowledge unavailable office=%s cause=%s",
                office_key,
                cause,
            )
            return {
                **result,
                "outcome": "temporary_failure",
                "answer": "Office knowledge is temporarily unavailable.",
            }
