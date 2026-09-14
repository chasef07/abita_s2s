"""Deliver Product lifecycle evidence; LiveKit owns transcripts and model metrics."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from importlib.metadata import version
from uuid import UUID

import httpx

from abita_s2s.config import Config
from abita_s2s.offices import get_office_profile
from abita_s2s.state import CallContext

logger = logging.getLogger(__name__)


class ReportingError(RuntimeError):
    """Product did not acknowledge the final call evidence."""


class CallReporter:
    def __init__(
        self,
        call: CallContext,
        client: httpx.AsyncClient,
        config: Config,
        drain: Callable[[], Awaitable[None]],
    ):
        self._client = client
        self._url = config.interaction_url
        self._secret = config.product_secret
        self._drain = drain
        self._base = {
            "officeKey": call.called_office_key,
            "officePhone": get_office_profile(call.called_office_key).trunk_numbers[0],
            "callerPhone": call.caller_phone,
            "sourceCallId": call.call_id,
            "startedAt": call.session_started_at.isoformat(),
        }
        self._appointment: dict | None = None
        self._facts: list[dict] = []
        self._pending: asyncio.Task | None = None
        self._finish_task: asyncio.Task | None = None
        self.started = False
        self.transfer_status = "idle"
        self._queue({**self._base, "kind": "START", "status": "IN_PROGRESS"})

    def record(self, kind: str, evidence: dict, *, call_id: str | None = None) -> None:
        """Retain application receipts alongside native tool executions, using Product's contract."""
        outcome = evidence["outcome"]
        if outcome in (
            "created",
            "duplicate",
            "updated",
            "booked",
            "cancelled",
            "rescheduled",
        ):
            status = "success"
        elif outcome in ("partial", "partial_booking", "partial_reschedule"):
            status = "partial"
        elif outcome in ("uncertain", "ambiguous"):
            status = "ambiguous"
        elif outcome in ("failed", "rejected", "invalid_receipt", "error"):
            status = "failed"
        else:
            status = "blocked"
        self._facts.append(
            {
                "callId": call_id,
                "outcome": f"{kind}_{outcome}",
                "status": status,
                "occurredAt": datetime.now(UTC).isoformat(),
                "evidence": deepcopy(evidence),
            }
        )

    def appointment(self, evidence: dict, *, call_id: str | None = None) -> None:
        """Checkpoint each observed scheduling write, including partial/uncertain results."""
        outcome = {**deepcopy(evidence), "occurredAt": datetime.now(UTC).isoformat()}
        self._appointment = outcome
        booking = evidence.get("bookingResult", {}).get("status")
        cancellation = evidence.get("cancellationResult", {}).get("status")
        if evidence["action"] == "RESCHEDULED":
            result = (
                "rescheduled"
                if booking == "booked" and cancellation == "cancelled"
                else "partial_reschedule"
                if booking in ("booked", "partial")
                else booking
            )
        else:
            result = booking or cancellation
        self.record("appointment", {**outcome, "outcome": result}, call_id=call_id)
        self._queue(
            {
                **self._base,
                "kind": "OUTCOME_CHECKPOINT",
                "status": "IN_PROGRESS",
                "appointmentOutcome": outcome,
            }
        )

    def _queue(self, payload: dict) -> None:
        # Ordered, retained tasks keep START/checkpoints ahead of CLOSEOUT even if a tool exits.
        previous = self._pending

        async def deliver():
            if previous:
                await previous
            return await self._send(payload)

        self._pending = asyncio.create_task(deliver())

    async def _send(self, payload: dict) -> bool:
        for attempt in range(2):
            try:
                async with asyncio.timeout(5):
                    response = await self._client.post(
                        self._url,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._secret}"},
                        timeout=5,
                        follow_redirects=False,
                    )
                if response.status_code in (200, 201):
                    receipt = response.json()
                    if (
                        isinstance(receipt, dict)
                        and isinstance(receipt.get("interactionId"), str)
                        and receipt.get("status") in ("created", "updated")
                    ):
                        UUID(receipt["interactionId"])
                        return True
                elif (
                    response.status_code not in (408, 429)
                    and response.status_code < 500
                ):
                    break
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                pass
            if attempt == 0:
                await asyncio.sleep(0.25)
        # Do not log transcripts, phones, credentials, response bodies, or backend identifiers.
        logger.error("Product call reporting failed kind=%s", payload["kind"])
        return False

    async def finish(self, make_report: Callable[[], dict] | None = None) -> None:
        """Drain accepted writes before one final snapshot. Shutdown may safely call again."""
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish(make_report))
        await asyncio.shield(self._finish_task)

    async def _finish(self, make_report: Callable[[], dict] | None) -> None:
        ended_at = datetime.now(UTC)
        async with asyncio.timeout(180):
            drain_failed = False
            try:
                await self._drain()
            except Exception:  # noqa: BLE001 - still deliver an explicit failed closeout
                drain_failed = True
                logger.error("Call mutation drain failed during reporting")
            report = None
            if make_report:
                try:
                    report = make_report()
                except Exception:  # noqa: BLE001 - never invent a substitute transcript
                    logger.error("Native session report unavailable")
            close = next(
                (
                    event
                    for event in reversed((report or {}).get("events", []))
                    if event.get("type") == "close"
                ),
                {},
            )
            if isinstance(close.get("created_at"), (int, float)):
                ended_at = datetime.fromtimestamp(close["created_at"], UTC)
            failed = (
                not self.started
                or report is None
                or not close
                or drain_failed
                or close.get("reason") == "error"
                or close.get("error") is not None
            )
            status = (
                "FAILED"
                if failed
                else (
                    "ESCALATED" if self.transfer_status == "accepted" else "COMPLETED"
                )
            )
            payload = {
                **self._base,
                "kind": "CLOSEOUT",
                "status": status,
                "endedAt": ended_at.isoformat(),
                "closeoutPayload": {
                    "agentVersion": version("abita-s2s"),
                    "closeReason": close.get("reason", "startup_or_report_failure"),
                    "mutationDrainFailed": drain_failed,
                    "transferStatus": self.transfer_status,
                    "domainOutcomes": deepcopy(self._facts),
                },
            }
            if report is not None:
                payload["transcript"] = report
            if self._appointment:
                payload["appointmentOutcome"] = self._appointment
            if self._pending:
                await self._pending
            if not await self._send(payload):
                raise ReportingError("Product did not acknowledge call closeout")
