"""Own handoff admission, single-attempt REFER, and safe call completion."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import RunContext, function_tool
from livekit.agents.beta.tools.end_call import EndCallTool

from abita_s2s.offices import get_office_profile
from abita_s2s.state import CallState

logger = logging.getLogger(__name__)


def result(outcome: str, answer: str) -> str:
    return json.dumps({"outcome": outcome, "answer": answer})


class CallControl(EndCallTool):
    """One per session; patient changes never reset an issued handoff."""

    def __init__(
        self, state: CallState, client: httpx.AsyncClient, room=None, sip=None
    ):
        super().__init__(
            delete_room=False, end_instructions="Say a brief goodbye to the caller."
        )
        self.state = state
        self.client = client
        self.room = room
        self.sip = sip
        self.status = "idle"
        self.attempts = 0
        self.ending = False
        self._payload = None
        self._admission_started = False
        self._refer_started = False

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
        if self.ending or self.attempts >= 2:
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
        self._admission_started = self._refer_started = False
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
            target, headers = await self._target()
            if not self._active():
                raise RuntimeError("Caller disconnected")
            self._refer_started = True
            response = await self.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.state.call.room_name,
                    participant_identity=self.state.call.sip_participant_identity,
                    transfer_to=target,
                    headers=headers,
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
                self.attempts = 2
                return result(
                    "failed",
                    "Provider reported transfer failure. Do not retry. Continue helping the caller.",
                )
            self.status = "ambiguous"
        except asyncio.CancelledError:
            self.status = (
                "ambiguous"
                if self._admission_started or self._refer_started
                else "failed"
            )
            raise
        except Exception as error:  # noqa: BLE001 - every provider failure must fence retries
            logger.warning(
                "Transfer incomplete phase=%s cause=%s",
                "refer"
                if self._refer_started
                else "admission"
                if self._admission_started
                else "pre_refer",
                type(error).__name__,
            )
            if self._admission_started or self._refer_started:
                self.status = "ambiguous"
            else:
                self.status = "failed"
                retry = (
                    " You may try once more." if self.attempts < 2 else " Do not retry."
                )
                return result("failed", "No SIP transfer was sent." + retry)
        return result(
            "ambiguous", "Transfer may be in progress. Do not retry or end the call."
        )

    async def _target(self) -> tuple[str, dict[str, str]]:
        call = self.state.call
        office = get_office_profile(call.called_office_key)
        if office.transfer_phone:
            return office.transfer_phone, {
                "X-Acuity-Caller-Phone": call.caller_phone or "",
                "X-Acuity-Handoff": "call-center",
                "X-Acuity-Handoff-Target": office.transfer_phone,
                "X-Acuity-LiveKit-Call-Id": call.call_id,
                "X-Acuity-Office-Key": office.key,
                "X-Acuity-Trunk-Phone": call.called_number or "",
            }
        product_url = os.environ.get("ACUITY_PRODUCT_HANDOFF_URL", "").strip()
        practice = os.environ.get("ABITA_EYE_GROUP_PRODUCT_PRACTICE_ID", "").strip()
        product = bool(product_url or practice)
        url = (
            product_url if product else os.environ.get("ACUITY_HANDOFF_URL", "").strip()
        )
        secret = os.environ.get(
            "ABITA_EYE_GROUP_PRODUCT_SERVICE_SECRET"
            if product
            else "ACUITY_HANDOFF_SECRET",
            "",
        ).strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or not secret
        ):
            raise ValueError("Handoff configuration is incomplete")
        if product:
            UUID(practice)
        if self._payload is None:
            if product:
                identity = {
                    "practiceId": practice,
                    "officeKey": office.key,
                    "sourceCallId": call.call_id,
                }
                contact = {
                    "phone": call.caller_phone or "",
                    "phoneSource": "livekit.sip.callerPhoneNumber",
                }
                if self.state.patient.active is not None:
                    contact.update(
                        displayName=self.state.patient.active.name,
                        nameSource="abita.patient-context",
                    )
                self._payload = {
                    **identity,
                    "contact": contact,
                    "idempotencyKey": hashlib.sha256(
                        json.dumps(identity, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            else:
                self._payload = {
                    "sourceCallId": call.call_id,
                    "routePhoneNumber": call.called_number,
                    "callerPhone": call.caller_phone,
                }
        headers = {"Authorization": f"Bearer {secret}"}
        if not product:
            headers["Idempotency-Key"] = hashlib.sha256(
                json.dumps(self._payload, separators=(",", ":")).encode()
            ).hexdigest()
        self._admission_started = True
        response = await self.client.post(
            url, json=self._payload, headers=headers, timeout=2
        )
        # An explicit rejection permits a bounded retry; conflict, timeout, server
        # errors, and malformed success can represent a committed admission.
        if 400 <= response.status_code < 500 and response.status_code not in (408, 409):
            self._admission_started = False
        response.raise_for_status()
        body = response.json()
        expires = datetime.fromisoformat(body["expiresAt"])
        remaining = (expires - datetime.now(UTC)).total_seconds()
        if remaining <= 0 or (product and remaining > 300):
            raise ValueError("Invalid handoff expiration")
        target = body["sipDestination" if product else "sipUri"]
        # Destination comes only from the authenticated office admission endpoint.
        if (
            not isinstance(target, str)
            or not target.startswith("sip:")
            or "@" not in target
            or any(c.isspace() for c in target)
        ):
            raise ValueError("Invalid SIP destination")
        if product:
            UUID(body["id"])
            if target[4:].split("@", 1)[0] != "acuity-handoff":
                raise ValueError("Invalid Product destination")
        elif body.get("type") != "DIRECT" or not body.get("handoffId"):
            raise ValueError("Invalid direct admission")
        return target, {}

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
