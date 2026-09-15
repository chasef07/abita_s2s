"""Own transfer sequencing, retry eligibility, and safe call completion."""

import asyncio
import json
import logging
import os

import httpx
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import RunContext, function_tool
from livekit.agents.beta.tools.end_call import EndCallTool

from abita_s2s.config import HandoffConfig
from abita_s2s.handoff import AdmissionRejected, HandoffAdmission
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


def result(outcome: str, answer: str) -> str:
    return json.dumps({"outcome": outcome, "answer": answer})


class CallControl(EndCallTool):
    """One per session; patient changes never reset an issued handoff."""

    def __init__(
        self,
        state: CallState,
        client: httpx.AsyncClient,
        room=None,
        sip=None,
        *,
        handoff: HandoffConfig | None = None,
    ):
        super().__init__(
            delete_room=False, end_instructions="Say a brief goodbye to the caller."
        )
        self.state = state
        self.admission = HandoffAdmission(state, client, handoff)
        self.room = room
        self.sip = sip
        self.status = "idle"
        self.attempts = 0
        self.ending = False

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

    @function_tool
    async def transfer_call(self, ctx: RunContext[CallState]) -> str:
        """Transfer to human staff when office policy requires it, including emergencies or named staff.

        No patient lookup is required. Call without announcing; this tool speaks first.
        Retry only if the result explicitly offers one retry. Never claim a human answered.
        """
        if os.environ.get("LIVEKIT_AGENT_DEPLOYMENT", "").strip():
            return result(
                "blocked",
                "Human transfers are unavailable in this sandbox call. No transfer was made.",
            )
        if self.status in ("pending", "accepted", "ambiguous"):
            return result(
                self.status,
                "Transfer may already be in progress. Do not retry or end the call.",
            )
        if self.ending or self.status == "failed":
            return result(
                "blocked", "Transfer is unavailable. No further retry is allowed."
            )
        if not self._active() or self.sip is None:
            return result(
                "unavailable",
                "No active SIP caller is available. No transfer was made.",
            )
        ctx.disallow_interruptions()
        self.status = "pending"
        self.attempts += 1
        phase = "preparing"
        try:
            await ctx.wait_for_playout()
            # GPT-Live has native speech and no standalone TTS. Explicit instructions
            # keep the announcement owned by this tool in the caller's language.
            speech = ctx.session.generate_reply(
                instructions="Say only: One moment while I transfer you to the office. Use the caller's language.",
                tool_choice="none",
                allow_interruptions=False,
            )
            await speech.wait_for_playout()
            if speech.interrupted or speech.exception() is not None:
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
                return result(
                    "accepted",
                    "Provider accepted the transfer. A human answer is not confirmed. Do not retry or end the call.",
                )
            if response.status == api.STS_TRANSFER_FAILED:
                self.status = "failed"
                # A definitive provider rejection is visible, but do not reissue an
                # admitted Product handoff or broaden the source's REFER retry policy.
                return result(
                    "failed",
                    "Provider reported transfer failure. Do not retry. Continue helping the caller.",
                )
            self.status = "ambiguous"
        except asyncio.CancelledError:
            self.status = (
                ("retryable" if self.attempts < 2 else "failed")
                if phase == "preparing"
                else "ambiguous"
            )
            raise
        except Exception as error:  # noqa: BLE001 - every provider failure must fence retries
            logger.warning(
                "Transfer incomplete phase=%s cause=%s", phase, type(error).__name__
            )
            if phase == "preparing" or (
                phase == "admitting" and isinstance(error, AdmissionRejected)
            ):
                self.status = "retryable" if self.attempts < 2 else "failed"
                retry = (
                    " You may try once more."
                    if self.status == "retryable"
                    else " Do not retry."
                )
                return result("failed", "No SIP transfer was sent." + retry)
            self.status = "ambiguous"
        return result(
            "ambiguous", "Transfer may be in progress. Do not retry or end the call."
        )

    async def _end_call(self, ctx: RunContext):
        if self.status in ("pending", "accepted", "ambiguous"):
            return result("blocked", "Transfer may be in progress. Do not hang up.")
        if self.ending:
            return result("pending", "Call completion is already in progress.")
        if not self._active():
            return result(
                "unavailable", "No active SIP call to end. The session remains open."
            )
        ctx.disallow_interruptions()
        self.ending = True
        return await super()._end_call(ctx)
