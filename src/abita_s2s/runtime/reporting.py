"""Deliver Product lifecycle evidence; LiveKit owns transcripts and model metrics."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from importlib.metadata import version
from uuid import UUID

import httpx

from abita_s2s.observability.evaluation import evaluate_call
from abita_s2s.config import Config
from abita_s2s.offices import get_office_profile, get_product_office_key
from abita_s2s.release import call_versions
from abita_s2s.state import CallContext

logger = logging.getLogger(__name__)
AGENT_VERSION = version("abita-s2s")
FINISH_SECONDS = 80
SEND_SECONDS = 5
RETRY_PAUSE_SECONDS = 0.25
CLOSEOUT_SECONDS = 2 * SEND_SECONDS + RETRY_PAUSE_SECONDS + 1


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
        self._gateway_key = config.ai_gateway_key
        self._drain = drain
        self._started_at = call.session_started_at
        office = get_office_profile(call.called_office_key)
        office_phone = call.called_number or office.trunk_numbers[0]
        self._base = {
            "officeKey": get_product_office_key(office_phone),
            "officePhone": office_phone,
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
        self.knowledge_revision: str | None = None
        self._versions = call_versions()
        self._queue({**self._base, "kind": "START", "status": "IN_PROGRESS"})

    def observe_knowledge_revision(self, revision_id: str) -> None:
        """Keep the latest office knowledge revision this call's searches used."""
        self.knowledge_revision = revision_id

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
            "verified",
            "switched",
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
                "outcome": outcome if kind == "appointment" else f"{kind}_{outcome}",
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
        previous = self._pending

        async def deliver():
            if previous:
                await previous
            return await self._send(payload)

        self._pending = asyncio.create_task(deliver())

    async def _send(self, payload: dict) -> bool:
        for attempt in range(2):
            try:
                async with asyncio.timeout(SEND_SECONDS):
                    response = await self._client.post(
                        self._url,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._secret}"},
                        timeout=SEND_SECONDS,
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
                await asyncio.sleep(RETRY_PAUSE_SECONDS)
        logger.error("Product call reporting failed kind=%s", payload["kind"])
        return False

    async def finish(self, make_report: Callable[[], dict] | None = None) -> None:
        """Drain accepted writes before one final snapshot. Shutdown may safely call again."""
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish(make_report))
        await asyncio.shield(self._finish_task)

    async def _settle_checkpoints(self, seconds: float) -> None:
        """Let queued checkpoints land before the closeout, but never delay it past budget."""
        if self._pending is None:
            return
        done, _ = await asyncio.wait({self._pending}, timeout=max(seconds, 0))
        if not done:
            self._pending.cancel()
            logger.error("Product checkpoints unfinished before closeout")

    async def _finish(self, make_report: Callable[[], dict] | None) -> None:
        """Drain, then evaluate and settle checkpoints together, keeping time to send."""
        ended_at = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FINISH_SECONDS
        async with asyncio.timeout_at(deadline):
            drain_failed = False
            try:
                await self._drain()
            except Exception:
                drain_failed = True
                logger.error("Call mutation drain failed during reporting")
            report = None
            if make_report:
                try:
                    report = make_report()
                except Exception:
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
                    "agentVersion": AGENT_VERSION,
                    "versions": {
                        **self._versions,
                        **(
                            {"knowledge": self.knowledge_revision}
                            if self.knowledge_revision
                            else {}
                        ),
                    },
                    "closeReason": close.get("reason", "startup_or_report_failure"),
                    "mutationDrainFailed": drain_failed,
                    "transferStatus": self.transfer_status,
                    "domainOutcomes": deepcopy(self._facts),
                },
            }
            if self._appointment:
                payload["appointmentOutcome"] = self._appointment
            remaining = deadline - loop.time() - CLOSEOUT_SECONDS
            settled = self._settle_checkpoints(remaining)
            if report is None:
                await settled
            else:
                payload["transcript"] = report
                payload["closeoutPayload"]["evaluation"], _ = await asyncio.gather(
                    evaluate_call(
                        report,
                        self._gateway_key,
                        (ended_at - self._started_at).total_seconds(),
                        budget=remaining,
                    ),
                    settled,
                )
            if not await self._send(payload):
                raise ReportingError("Product did not acknowledge call closeout")
