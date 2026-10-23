"""Product staff tasks, office knowledge and handoff admission over the shared transport."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

import httpx
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field,
    model_validator,
)

from abita_s2s.config import Config
from abita_s2s.integrations.transport import Fault, Reply, Request, post
from abita_s2s.records import Record

STAFF_TASK_TIMEOUT = 10.0
SEARCH_TIMEOUT = 4.0
HANDOFF_TIMEOUT = 2.0

Category = Literal[
    "appointments",
    "documentation",
    "medication",
    "optical",
    "referrals",
    "other",
    "insurance",
    "pre_op",
    "post_op",
]
Urgency = Literal["high_priority", "normal", "non_urgent"]


def _uuid(value: str) -> str:
    UUID(value)
    return value


class StaffTask(Request):
    callId: str
    callerPhone: str
    category: Category
    message: str
    officePhone: str
    source: Literal["agent"]
    summary: str
    urgency: Urgency
    patient: dict[str, str] | None = None
    inboundOfficePhone: str | None = None
    officeKey: str

    @computed_field
    @property
    def idempotencyKey(self) -> str:
        task = self.model_dump(exclude={"idempotencyKey"}, exclude_none=True)
        digest = hashlib.sha256(json.dumps(task, sort_keys=True).encode())
        return "staff_task_" + digest.hexdigest()


class StaffTaskReceipt(Record):
    status: Literal["created", "duplicate"]
    taskId: Annotated[str, AfterValidator(_uuid)]
    category: Category
    urgency: Urgency


class KnowledgeQuery(Request):
    query: str


_Id = Annotated[str, Field(min_length=1, max_length=100)]


class Passage(BaseModel):
    model_config = ConfigDict(strict=True)
    revisionId: _Id
    sectionId: _Id
    title: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=12000)]


class KnowledgeResult(BaseModel):
    model_config = ConfigDict(strict=True)
    outcome: Literal["found", "no_relevant_information", "temporary_failure"]
    revisionId: _Id | None = None
    passages: Annotated[list[Passage], Field(max_length=8)]

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


class HandoffContact(Request):
    phone: str
    phoneSource: Literal["livekit.sip.callerPhoneNumber"]


class HandoffRequest(Request):
    practiceId: str
    officeKey: str
    sourceCallId: str
    contact: HandoffContact

    @computed_field
    @property
    def idempotencyKey(self) -> str:
        identity = {
            "practiceId": self.practiceId,
            "officeKey": self.officeKey,
            "sourceCallId": self.sourceCallId,
        }
        return hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()


class HandoffGrant(Record):
    id: Annotated[str, AfterValidator(_uuid)]
    sipDestination: str
    expiresAt: str

    @model_validator(mode="after")
    def admitted(self) -> Self:
        expires = datetime.fromisoformat(self.expiresAt)
        if expires.tzinfo is None:
            raise ValueError("Invalid handoff expiration")
        remaining = (expires - datetime.now(UTC)).total_seconds()
        if remaining <= 0 or remaining > 300:
            raise ValueError("Invalid handoff expiration")
        target = self.sipDestination
        if (
            not target.startswith("sip:")
            or "@" not in target
            or any(c.isspace() for c in target)
        ):
            raise ValueError("Invalid SIP destination")
        if target[4:].split("@", 1)[0] != "acuity-handoff":
            raise ValueError("Invalid Product destination")
        return self


STAFF_TASK_RECEIPT = TypeAdapter(StaffTaskReceipt)
KNOWLEDGE_RESULT = TypeAdapter(KnowledgeResult)
HANDOFF_GRANT = TypeAdapter(HandoffGrant)


class Product:
    """Bearer-authenticated Product endpoints; each method owns its retry policy."""

    def __init__(self, client: httpx.AsyncClient, config: Config):
        self._client = client
        self._config = config

    @property
    def staff_tasks_configured(self) -> bool:
        return bool(self._config.staff_tasks_url and self._config.product_secret)

    async def submit(
        self, task: StaffTask
    ) -> StaffTaskReceipt | Literal["rejected", "ambiguous"]:
        """Deliver at most twice under one idempotency key; unconfirmed is ambiguous."""
        uncertain = False
        for _ in range(2):
            reply = await self._post(
                self._config.staff_tasks_url,
                self._config.product_secret,
                task,
                STAFF_TASK_RECEIPT,
                STAFF_TASK_TIMEOUT,
            )
            if reply == "not_configured":
                return "rejected"
            if isinstance(reply, str) or reply.retryable:
                uncertain = True
                continue
            if reply.code not in (200, 201):
                if uncertain:
                    break
                return "rejected"
            receipt = reply.body
            if (
                receipt is None
                or receipt.category != task.category
                or receipt.urgency != task.urgency
            ):
                uncertain = True
                continue
            return receipt
        return "ambiguous"

    async def search(self, office_key: str, query: str) -> KnowledgeResult | str:
        """The corpus answer, or a log-safe cause when there is none."""
        reply = await self._post(
            self._config.knowledge_url,
            self._config.product_secret,
            KnowledgeQuery(query=query),
            KNOWLEDGE_RESULT,
            SEARCH_TIMEOUT,
            {"X-Office-Key": office_key},
        )
        if isinstance(reply, str):
            return reply
        if not reply.ok:
            return f"http_{reply.code}"
        if reply.body is None:
            return "invalid_response"
        return reply.body

    async def admit(
        self, office_key: str, source_call_id: str, caller_phone: str
    ) -> HandoffGrant | Literal["not_configured", "rejected", "unconfirmed"]:
        """Admit one handoff; a definite 4xx is the only failure known not to write."""
        handoff = self._config.handoff
        if handoff is None:
            return "not_configured"
        request = HandoffRequest(
            practiceId=handoff.practice_id,
            officeKey=office_key,
            sourceCallId=source_call_id,
            contact=HandoffContact(
                phone=caller_phone, phoneSource="livekit.sip.callerPhoneNumber"
            ),
        )
        reply = await self._post(
            handoff.url, handoff.secret, request, HANDOFF_GRANT, HANDOFF_TIMEOUT
        )
        if reply == "not_configured":
            return "not_configured"
        if isinstance(reply, str):
            return "unconfirmed"
        if 400 <= reply.code < 500 and reply.code not in (408, 409):
            return "rejected"
        if not reply.ok or reply.body is None:
            return "unconfirmed"
        return reply.body

    async def _post[T](
        self,
        url: str | None,
        secret: str | None,
        request: Request,
        reply: TypeAdapter[T],
        deadline: float,
        headers: dict[str, str] | None = None,
    ) -> Reply[T] | Fault:
        if not url or not secret:
            return "not_configured"
        return await post(
            self._client,
            url,
            {"Authorization": f"Bearer {secret}", **(headers or {})},
            request,
            reply,
            deadline,
        )
