"""Own transfer sequencing, retry eligibility, and safe call completion."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc

from abita_s2s.config import HandoffConfig
from abita_s2s.handoff import AdmissionRejected, HandoffAdmission
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)
AMBIGUOUS = "ambiguous: Transfer may be in progress. Do not retry or end the call."


class CallControl:
    """One per session; patient changes never reset an issued handoff."""

    def __init__(
        self,
        state: CallState,
        client: httpx.AsyncClient,
        room=None,
        sip=None,
        *,
        handoff: HandoffConfig | None = None,
        sandbox: bool = False,
        simulation: bool = False,
    ):
        self.state = state
        self.sandbox = sandbox
        self.simulation = simulation
        self.admission = HandoffAdmission(state, client, handoff)
        self.room = room
        self.sip = sip
        self.status = "idle"
        self.attempts = 0
        self.ending = False
        self._closed = False
        self._transfer_task = None

    def close_admission(self):
        self._closed = True

    async def aclose(self):
        self.close_admission()
        if self._transfer_task:
            await asyncio.shield(self._transfer_task)

    def _active(self) -> bool:
        call = self.state.call
        participant = (
            self.room.remote_participants.get(call.sip_participant_identity)
            if self.room
            else None
        )
        return bool(
            self.room
            and self.room.isconnected()
            and self.room.name == call.room_name
            and participant
            and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        )

    def _retry_status(self) -> str:
        return "retryable" if self.attempts < 2 else "failed"

    def _not_sent(self) -> str:
        if self.status == "retryable":
            return "failed: No SIP transfer was sent. You may try once more."
        return "failed: No SIP transfer was sent. Do not retry."

    async def transfer(
        self, hold: Callable[[], None], announce: Callable[[], Awaitable[bool]]
    ) -> str:
        """Transfer the SIP caller; hold fences interruptions, announce speaks first."""
        if self._closed:
            return "blocked: This call has ended."
        if self._transfer_task and not self._transfer_task.done():
            return "pending: Transfer is already in progress."
        self._transfer_task = asyncio.create_task(self._transfer(hold, announce))
        return await asyncio.shield(self._transfer_task)

    async def _transfer(self, hold, announce):
        deadline = asyncio.timeout(40)
        try:
            async with deadline:
                return await self._perform_transfer(hold, announce)
        except TimeoutError:
            if not deadline.expired():
                raise
            logger.warning("Transfer deadline expired status=%s", self.status)
            if self.status == "ambiguous":
                return AMBIGUOUS
            return self._not_sent()

    async def _perform_transfer(self, hold, announce):
        if self.sandbox:
            return "blocked: Human transfers are unavailable in this sandbox call. No transfer was made."
        if self.status in ("pending", "accepted", "ambiguous"):
            return f"{self.status}: Transfer may already be in progress. Do not retry or end the call."
        if self.ending or self.status == "failed":
            return "blocked: Transfer is unavailable. No further retry is allowed."
        if not self._active() or self.sip is None:
            return (
                "unavailable: No active SIP caller is available. No transfer was made."
            )
        hold()
        self.status = "pending"
        self.attempts += 1
        phase = "preparing"
        try:
            if not await announce():
                raise RuntimeError("Announcement did not complete")
            if not self._active():
                raise RuntimeError("Caller disconnected")
            phase = "admitting"
            target = await self.admission.resolve()
            if not target.admitted:
                phase = "preparing"
            if not self._active():
                raise RuntimeError("Caller disconnected")
            phase = "transferring"
            response = await self.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.state.call.room_name,
                    participant_identity=self.state.call.sip_participant_identity,
                    transfer_to=target.destination,
                    headers=target.headers,
                    play_dialtone=True,
                    ringing_timeout=Duration(seconds=20),
                ),
                timeout=30,
            )
            if response.status == api.STS_TRANSFER_SUCCESSFUL:
                self.status = "accepted"
                return "accepted: Provider accepted the transfer. A human answer is not confirmed. Do not retry or end the call."
            if response.status == api.STS_TRANSFER_FAILED:
                self.status = "failed"
                return "failed: Provider reported transfer failure. Do not retry. Continue helping the caller."
            self.status = "ambiguous"
        except asyncio.CancelledError:
            self.status = self._retry_status() if phase == "preparing" else "ambiguous"
            raise
        except Exception as error:
            logger.warning(
                "Transfer incomplete phase=%s cause=%s", phase, type(error).__name__
            )
            if phase == "preparing" or (
                phase == "admitting" and isinstance(error, AdmissionRejected)
            ):
                self.status = self._retry_status()
                return self._not_sent()
            self.status = "ambiguous"
        finally:
            if self.state.reporter:
                self.state.reporter.transfer_status = self.status
        return AMBIGUOUS

    def begin_end(self, hold: Callable[[], None]) -> str | None:
        """Admit call completion, or return why the call must stay open."""
        if self.status in ("pending", "accepted", "ambiguous"):
            return "blocked: Transfer may be in progress. Do not hang up."
        if self.ending:
            return "pending: Call completion is already in progress."
        if not self.simulation and not self._active():
            return "unavailable: No active SIP call to end. The session remains open."
        hold()
        self.ending = True
        return None
