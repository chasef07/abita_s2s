"""Editable per-call staff requests, delivered once at call closeout."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, get_args
from uuid import UUID, uuid4

import httpx

from abita_s2s.config import Config
from abita_s2s.identity import PatientResolver
from abita_s2s.offices import get_office_profile, get_product_office_key
from abita_s2s.state import CallState

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


def _phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if re.fullmatch(r"[1-9][0-9]{7,14}", digits) else ""


def _result(outcome: str, answer: str) -> dict:
    return {"outcome": outcome, "answer": answer}


@dataclass(frozen=True, repr=False)
class Draft:
    payload: dict
    call_id: str | None


class StaffTasks:
    def __init__(
        self,
        state: CallState,
        resolver: PatientResolver,
        client: httpx.AsyncClient,
        config: Config,
    ):
        self.state = state
        self._resolver = resolver
        self._client = client
        self._url = config.staff_tasks_url
        self._secret = config.product_secret
        self._drafts: dict[str, Draft] = {}
        self._close_task: asyncio.Task | None = None
        self._closed = False

    def close_admission(self) -> None:
        self._closed = True

    def save(
        self,
        category: Category,
        urgency: Urgency,
        summary: str,
        message: str,
        *,
        draft_id: str | None = None,
        call_id: str | None = None,
    ) -> dict:
        if self._closed:
            return _result("failed", "This call has ended. No draft was saved.")
        if draft_id is not None and draft_id not in self._drafts:
            return _result("failed", "Unknown draft ID. No draft was saved.")
        try:
            payload = self._payload(category, urgency, summary, message)
        except ValueError as exc:
            return _result("failed", f"{exc} Existing drafts are unchanged.")
        patient = payload.get("patient")
        if draft_id is not None:
            if self._drafts[draft_id].payload.get("patient") != patient:
                return _result(
                    "failed",
                    "Patient context changed. Existing draft is unchanged. "
                    "For a different patient, create a separate draft; to correct "
                    "the original patient's identity, discard that draft and save it again.",
                )
        else:
            draft_id = (
                next(
                    (
                        key
                        for key, draft in self._drafts.items()
                        if draft.payload == payload
                    ),
                    None,
                )
                or uuid4().hex
            )
        self._drafts[draft_id] = Draft(payload, call_id)
        return {
            **_result(
                "saved", "Draft saved for submission when the call ends. Not yet sent."
            ),
            "draftId": draft_id,
            "summary": payload["summary"],
            "patient": (
                {"name": patient.get("name"), "verified": "id" in patient}
                if patient
                else None
            ),
        }

    def discard(self, draft_id: str) -> dict:
        if self._closed:
            return _result(
                "failed", "This call has ended. Drafts can no longer be changed."
            )
        if self._drafts.pop(draft_id, None) is None:
            return _result("failed", "Unknown draft ID. No draft was removed.")
        return _result("discarded", "Draft removed. This request will not be sent.")

    async def aclose(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._flush())
        await asyncio.shield(self._close_task)

    async def _flush(self) -> None:
        # Freeze final payloads once; exact repeats and HTTP retries share a key.
        deliveries = {}
        for draft in self._drafts.values():
            payload = dict(draft.payload)
            key = (
                "staff_task_"
                + hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest()
            )
            payload["idempotencyKey"] = key
            deliveries.setdefault(key, (payload, draft.call_id))
        results = await asyncio.gather(
            *(
                self._deliver(payload, call_id=call_id)
                for payload, call_id in deliveries.values()
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise RuntimeError("An accepted staff delivery was cancelled")
            if isinstance(result, BaseException):
                raise result
            if result["outcome"] not in ("created", "duplicate"):
                raise RuntimeError("Staff delivery was not confirmed")

    def _payload(
        self, category: Category, urgency: Urgency, summary: str, message: str
    ) -> dict:
        """Validate before any write, then snapshot the exact request for replay."""
        call = self.state.call
        if call.customer_key != "abita":
            raise ValueError(
                "Staff tasks are unavailable for this call. No request was sent."
            )
        try:
            office = get_office_profile(call.called_office_key)
        except ValueError as exc:
            raise ValueError(
                "This office is not authorized. No request was sent."
            ) from exc
        if not office.staff_tasks_enabled:
            raise ValueError(
                "Staff tasks are disabled for this office. No request was sent."
            )
        if not self._url or not self._secret:
            raise ValueError("Staff delivery is not configured. No request was sent.")
        if category not in get_args(Category):
            raise ValueError(
                "Product does not support this category. No request was sent. Do not relabel it to bypass this restriction; offer office help."
            )
        summary, message = summary.strip(), message.strip()
        if (
            urgency not in ("high_priority", "normal", "non_urgent")
            or not 1 <= len(summary) <= 240
            or not 1 <= len(message) <= 2500
        ):
            raise ValueError(
                "No request was sent. Use a supported urgency, a summary up to 240 characters and details up to 2500 characters. Preserve essential intake and missing details."
            )
        phone = _phone(call.caller_phone)
        if not phone or not 1 <= len(call.call_id) <= 255:
            raise ValueError(
                "A valid caller contact and call identity are required. No request was sent; offer office help."
            )
        patient = self._resolver.staff_task_patient()
        if patient and any(
            len(v) > {"id": 255, "name": 200, "dob": 64}[k] for k, v in patient.items()
        ):
            raise ValueError(
                "Patient details exceed the delivery contract. No request was sent."
            )
        payload = {
            "callId": call.call_id,
            "callerPhone": phone,
            "category": category,
            "message": message,
            "officePhone": office.trunk_numbers[0],
            "source": "agent",
            "summary": summary,
            "urgency": urgency,
        }
        if patient:
            payload["patient"] = patient
        inbound = _phone(call.called_number)
        if call.called_number and inbound != office.trunk_numbers[0]:
            if inbound not in office.trunk_numbers:
                raise ValueError(
                    "Inbound office does not match this office. No request was sent."
                )
            payload["inboundOfficePhone"] = inbound
        payload["officeKey"] = get_product_office_key(
            inbound or office.trunk_numbers[0]
        )
        return payload

    async def _deliver(self, payload: dict, *, call_id: str | None = None) -> dict:
        result = await self._send(payload)
        if self.state.reporter:
            evidence = {
                "outcome": result["outcome"],
                "category": payload["category"],
                "urgency": payload["urgency"],
            }
            if "taskId" in result:
                evidence["taskId"] = result["taskId"]
            self.state.reporter.record("staff_task", evidence, call_id=call_id)
        return result

    async def _send(self, payload: dict) -> dict:
        uncertain = False
        for _ in range(2):
            try:
                async with asyncio.timeout(10.0):
                    response = await self._client.post(
                        self._url,
                        headers={"Authorization": f"Bearer {self._secret}"},
                        json=payload,
                        timeout=10.0,
                        follow_redirects=False,
                    )
                if response.status_code in (408, 429) or response.status_code >= 500:
                    uncertain = True
                    continue
                if response.status_code not in (200, 201):
                    if uncertain:
                        break
                    return _result(
                        "failed",
                        "Product rejected this request.",
                    )
                receipt = response.json()
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("status") not in ("created", "duplicate")
                    or not isinstance(receipt.get("taskId"), str)
                    or receipt.get("category") != payload["category"]
                    or receipt.get("urgency") != payload["urgency"]
                ):
                    uncertain = True
                    continue
                UUID(receipt["taskId"])
                return {
                    **_result(
                        receipt["status"],
                        "The request was sent to the team for review."
                        if receipt["status"] == "created"
                        else "This request was already sent to the team for review.",
                    ),
                    "taskId": receipt["taskId"],
                }
            except (httpx.HTTPError, ValueError, TimeoutError):
                uncertain = True
                continue
        return _result(
            "ambiguous",
            "Delivery could not be confirmed; the request may already have reached staff.",
        )
