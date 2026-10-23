"""Editable per-call staff requests, delivered once at call closeout."""

import asyncio
from dataclasses import dataclass
from typing import Literal, get_args
from uuid import uuid4

from abita_s2s.identity import PatientResolver
from abita_s2s.integrations.product import (
    Category,
    Product,
    StaffTask,
    StaffTaskReceipt,
    Urgency,
)
from abita_s2s.name_matcher import names_match, phone_name_matches, same_patient
from abita_s2s.offices import (
    e164,
    get_office_profile,
    get_product_office_key,
    us_trunk_number,
)
from abita_s2s.results import reply
from abita_s2s.state import CallState


def _same_patient(old: dict | None, new: dict | None) -> bool:
    """A draft may follow its patient as details are added, never move to another one.

    Unverified details may only be extended. A named, unverified patient may move to
    the verified chart that matches that name and any DOB already given.
    """
    if old == new:
        return True
    if not old or not new or "id" in old:
        return False
    if "id" not in new:
        return old.items() <= new.items()
    name = old.get("name")
    return bool(name) and same_patient(
        name,
        old.get("dob"),
        new["name"],
        new["dob"],
        lambda said, chart: names_match(said, chart) or phone_name_matches(said, chart),
    )


@dataclass(frozen=True, repr=False)
class Draft:
    task: StaffTask
    call_id: str | None


class StaffTasks:
    def __init__(
        self,
        state: CallState,
        resolver: PatientResolver,
        product: Product,
    ):
        self.state = state
        self._resolver = resolver
        self._product = product
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
        cancel: bool = False,
        call_id: str | None = None,
    ) -> dict:
        if self._closed:
            return reply("failed", "This call has ended. No draft was saved.")
        if draft_id is not None and draft_id not in self._drafts:
            return reply("failed", "Unknown draft ID. No draft was saved.")
        if cancel:
            if draft_id is None:
                return reply("failed", "A draft ID is required to cancel a request.")
            del self._drafts[draft_id]
            return reply("cancelled", "Request cancelled. It will not be submitted.")
        try:
            task = self._task(category, urgency, summary, message)
        except ValueError as exc:
            return reply("failed", f"{exc} Existing drafts are unchanged.")
        patient = task.patient
        if draft_id is not None:
            if not _same_patient(self._drafts[draft_id].task.patient, patient):
                return reply(
                    "failed",
                    "Patient context changed. Existing draft is unchanged. "
                    "For a different patient, create a separate draft; to correct "
                    "the original patient's identity, cancel that draft and save it again.",
                )
        else:
            draft_id = (
                next(
                    (key for key, draft in self._drafts.items() if draft.task == task),
                    None,
                )
                or uuid4().hex
            )
        self._drafts[draft_id] = Draft(task, call_id)
        return {
            **reply(
                "saved", "Draft saved for submission when the call ends. Not yet sent."
            ),
            "draftId": draft_id,
            "summary": task.summary,
            "patient": (
                {"name": patient.get("name"), "verified": "id" in patient}
                if patient
                else None
            ),
        }

    async def aclose(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._flush())
        await asyncio.shield(self._close_task)

    async def _flush(self) -> None:
        deliveries = {}
        for draft in self._drafts.values():
            deliveries.setdefault(draft.task.idempotencyKey, draft)
        results = await asyncio.gather(
            *(
                self._deliver(draft.task, call_id=draft.call_id)
                for draft in deliveries.values()
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

    def _task(
        self, category: Category, urgency: Urgency, summary: str, message: str
    ) -> StaffTask:
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
        if not self._product.staff_tasks_configured:
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
        phone = e164(call.caller_phone)
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
        inbound = us_trunk_number(call.called_number)
        other_trunk = bool(call.called_number) and inbound != office.trunk_numbers[0]
        if other_trunk and inbound not in office.trunk_numbers:
            raise ValueError(
                "Inbound office does not match this office. No request was sent."
            )
        return StaffTask(
            callId=call.call_id,
            callerPhone=phone,
            category=category,
            message=message,
            officePhone=office.trunk_numbers[0],
            source="agent",
            summary=summary,
            urgency=urgency,
            patient=patient or None,
            inboundOfficePhone=inbound if other_trunk else None,
            officeKey=get_product_office_key(inbound or office.trunk_numbers[0]),
        )

    async def _deliver(self, task: StaffTask, *, call_id: str | None = None) -> dict:
        result = _result(await self._product.submit(task))
        if self.state.reporter:
            evidence = {
                "outcome": result["outcome"],
                "category": task.category,
                "urgency": task.urgency,
            }
            if "taskId" in result:
                evidence["taskId"] = result["taskId"]
            self.state.reporter.record("staff_task", evidence, call_id=call_id)
        return result


def _result(delivery: StaffTaskReceipt | Literal["rejected", "ambiguous"]) -> dict:
    if delivery == "rejected":
        return reply("failed", "Product rejected this request.")
    if not isinstance(delivery, StaffTaskReceipt):
        return reply(
            "ambiguous",
            "Delivery could not be confirmed; the request may already have reached staff.",
        )
    return {
        **reply(
            delivery.status,
            "The request was sent to the team for review."
            if delivery.status == "created"
            else "This request was already sent to the team for review.",
        ),
        "taskId": delivery.taskId,
    }
