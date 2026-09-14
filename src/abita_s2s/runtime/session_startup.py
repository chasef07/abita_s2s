"""Resolve the office and compose one voice session per LiveKit job."""

import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from livekit import rtc
from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.config import load_config, load_middleware_config
from abita_s2s.middleware import MiddlewareClient
from abita_s2s.model_config import create_model
from abita_s2s.offices import (
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.runtime.precall_lookup import precall_lookup
from abita_s2s.state import CallContext, CallState


async def start_voice_call(ctx: JobContext) -> None:
    config = load_config()
    session_started_at = datetime.now(timezone.utc)
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
        participant = await ctx.wait_for_participant(
            kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        )
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

    state = CallState(call=call)
    http: httpx.AsyncClient | None = None
    lookup: asyncio.Task[None] | None = None
    session: AgentSession[CallState] | None = None

    def cancel_lookup(*_: object) -> None:
        if lookup is not None:
            lookup.cancel()

    def caller_disconnected(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == call.sip_participant_identity:
            cancel_lookup()

    async def close_middleware() -> None:
        if http is not None:
            ctx.room.off("participant_disconnected", caller_disconnected)
        if session is not None and lookup is not None:
            session.off("close", cancel_lookup)
        if lookup is not None:
            lookup.cancel()
            await asyncio.gather(lookup, return_exceptions=True)
        if http is not None:
            await http.aclose()

    try:
        if call.caller_phone:
            middleware_config = load_middleware_config(console=ctx.is_fake_job())
            http = httpx.AsyncClient(follow_redirects=False)
            middleware = MiddlewareClient(
                http,
                base_url=middleware_config.base_url,
                auth_token=middleware_config.auth_token,
            )
            lookup = asyncio.create_task(
                precall_lookup(
                    state,
                    middleware,
                    office=middleware_config.office_override
                    or office.middleware_office,
                )
            )
            ctx.room.on("participant_disconnected", caller_disconnected)
            ctx.add_shutdown_callback(close_middleware)

        session = AgentSession[CallState](
            userdata=state,
            llm=create_model(config),
            vad=None,
            turn_handling={"turn_detection": "realtime_llm"},
        )
        if lookup is not None:
            session.on("close", cancel_lookup)
        # LiveKit owns voice shutdown; lookup runs without delaying session.start.
        await session.start(
            agent=AbitaAgent(office),
            room=ctx.room,
            room_options=room_options,
        )
        if lookup is not None:
            await lookup
    except BaseException:
        await close_middleware()
        raise
