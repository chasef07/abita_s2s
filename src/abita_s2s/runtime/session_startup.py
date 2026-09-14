"""Resolve the office and compose one voice session per LiveKit job."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from livekit import rtc
from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import load_config
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.model_config import create_model
from abita_s2s.offices import (
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.state import CallContext, CallState


async def start_voice_call(ctx: JobContext) -> None:
    config = load_config()
    session_started_at = datetime.now(UTC)
    room_options = room_io.RoomOptions(
        close_on_disconnect=True,
        delete_room_on_close=True,
    )
    if ctx.is_fake_job():
        office_key = os.environ.get("ABITA_CONSOLE_OFFICE", "").strip()
        if not office_key:
            raise ValueError("Console mode requires ABITA_CONSOLE_OFFICE")
        office = get_office_profile(office_key)
        call = CallContext(
            call_id=f"console-{uuid4()}",
            session_started_at=session_started_at,
            customer_key="abita",
            called_office_key=office.key,
        )
    else:
        await ctx.connect()
        participant = await ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
        office = get_office_profile_by_phone(
            participant.attributes.get("sip.trunkPhoneNumber", "")
        )
        room_options.participant_identity = participant.identity
        attrs = participant.attributes
        sip_call_id = attrs.get("sip.callID") or None
        call = CallContext(
            call_id=sip_call_id or ctx.room.name or participant.identity,
            session_started_at=session_started_at,
            customer_key="abita",
            called_office_key=office.key,
            caller_phone=attrs.get("sip.phoneNumber") or None,
            called_number=attrs["sip.trunkPhoneNumber"],
            room_name=ctx.room.name,
            sip_participant_identity=participant.identity,
            sip_call_id=sip_call_id,
        )

    client = httpx.AsyncClient()
    ctx.add_shutdown_callback(client.aclose)
    session = AgentSession[CallState](
        userdata=CallState(call=call),
        llm=create_model(config),
        vad=None,
        turn_handling={"turn_detection": "realtime_llm"},
    )
    # LiveKit owns session shutdown and closes when the selected caller leaves.
    await session.start(
        agent=AbitaAgent(office, OfficeKnowledge(client, config)),
        room=ctx.room,
        room_options=room_options,
    )
