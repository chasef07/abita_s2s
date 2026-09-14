"""One call's authenticated staff delivery, exact replay, and durable receipts."""

import asyncio
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

import httpx

from abita_s2s.config import Config
from abita_s2s.identity import PatientResolver
from abita_s2s.offices import get_office_profile
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
CATEGORIES = {
    "appointments",
    "documentation",
    "medication",
    "optical",
    "referrals",
    "other",
}


def _phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if re.fullmatch(r"[1-9][0-9]{7,14}", digits) else ""


def _result(outcome: str, answer: str) -> dict:
    return {"outcome": outcome, "answer": answer}


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
        self._deliveries: dict[str, asyncio.Task] = {}
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        # A mutation already dispatched must finish and retain its receipt.
        await asyncio.gather(*self._deliveries.values(), return_exceptions=True)

    async def submit(
        self, category: Category, urgency: Urgency, summary: str, message: str
    ) -> dict:
        call = self.state.call
        if self._closed or call.customer_key != "abita":
            return _result(
                "failed",
                "Staff tasks are unavailable for this call. No request was sent.",
            )
        try:
            office = get_office_profile(call.called_office_key)
        except ValueError:
            return _result(
                "failed", "This office is not authorized. No request was sent."
            )
        if not office.staff_tasks_enabled:
            return _result(
                "failed",
                "Staff tasks are disabled for this office. No request was sent.",
            )
        if not self._url or not self._secret:
            return _result(
                "failed", "Staff delivery is not configured. No request was sent."
            )
        if category not in CATEGORIES:
            return _result(
                "failed",
                "Product does not support this category. No request was sent. Do not relabel it to bypass this restriction; offer office help.",
            )
        summary, message = summary.strip(), message.strip()
        if (
            urgency not in ("high_priority", "normal", "non_urgent")
            or not 1 <= len(summary) <= 240
            or not 1 <= len(message) <= 2500
        ):
            return _result(
                "failed",
                "No request was sent. Use a supported urgency, a summary up to 240 characters and details up to 2500 characters. Preserve essential intake and missing details.",
            )
        phone = _phone(call.caller_phone)
        if not phone or not 1 <= len(call.call_id) <= 255:
            return _result(
                "failed",
                "A valid caller contact and call identity are required. No request was sent; offer office help.",
            )
        patient = self._resolver.staff_task_patient()
        if patient and any(
            len(v) > {"id": 255, "name": 200, "dob": 64}[k] for k, v in patient.items()
        ):
            return _result(
                "failed",
                "Patient details exceed the delivery contract. No request was sent.",
            )
        payload = {
            "callId": call.call_id,
            "callerPhone": phone,
            "category": category,
            "message": message,
            "officeKey": office.key,
            "officePhone": office.trunk_numbers[0],
            "source": "agent",
            "summary": summary,
            "urgency": urgency,
        }
        if patient:
            payload["patient"] = patient
        if call.called_number and _phone(call.called_number) != office.trunk_numbers[0]:
            inbound = _phone(call.called_number)
            if inbound not in office.trunk_numbers:
                return _result(
                    "failed",
                    "Inbound office does not match this office. No request was sent.",
                )
            payload["inboundOfficePhone"] = inbound
        # Product fingerprints exact fields, including urgency and patient context.
        key = (
            "staff_task_"
            + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )
        payload["idempotencyKey"] = key
        task = self._deliveries.get(key)
        replay = task is not None
        if task is None or (
            task.done() and task.result()["outcome"] not in ("created", "duplicate")
        ):
            task = asyncio.create_task(
                self._deliver(
                    payload,
                    uncertain=task is not None
                    and task.result()["outcome"] == "ambiguous",
                )
            )
            self._deliveries[key] = task
            replay = False
        # Corrections/cancellation do not undo a submitted mutation or lose its receipt.
        result = dict(await asyncio.shield(task))
        if replay and result["outcome"] == "created":
            result.update(
                _result(
                    "duplicate", "This request was already sent to the team for review."
                )
            )
        result["summary"] = summary
        result["patient"] = (
            {"name": patient.get("name"), "verified": "id" in patient}
            if patient
            else None
        )
        if patient != self._resolver.staff_task_patient():
            result["patientChanged"] = True
            result["answer"] += (
                " This receipt belongs to the previous patient context, not the current patient."
            )
        return result

    async def _deliver(self, payload: dict, *, uncertain: bool = False) -> dict:
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
                        "Product rejected this request. Do not confirm submission; offer office help.",
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
            "Delivery could not be confirmed; the request may already have reached staff. Do not claim success or that nothing was sent. An identical retry can recover the receipt; do not change details just to retry.",
        )
