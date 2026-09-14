"""Resolve the office and compose one voice session per LiveKit job."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from livekit import api, rtc
from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.call_control import CallControl
from abita_s2s.config import load_config
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.model_config import create_model
from abita_s2s.offices import (
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.registration_middleware import RegistrationMiddleware
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP
from abita_s2s.staff_tasks import StaffTasks
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

    client = httpx.AsyncClient()
    insurance: InsuranceRegistration | None = None
    scheduling: Scheduling | None = None
    staff_tasks: StaffTasks | None = None

    async def close_client() -> None:
        # Close admission to every write owner, then drain all writes before HTTP.
        try:
            results = await asyncio.gather(
                *(owner.aclose() for owner in (scheduling, insurance, staff_tasks) if owner is not None),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            await client.aclose()

    ctx.add_shutdown_callback(close_client)
    state = CallState(call=call)
    session = AgentSession[CallState](
        userdata=state,
        llm=create_model(config),
        vad=None,
        turn_handling={"turn_detection": "realtime_llm"},
    )
    resolver = PatientResolver(state, PatientMiddleware(client, config))
    ctx.add_shutdown_callback(resolver.aclose)
    sip_api = None
    if not ctx.is_fake_job():
        sip_api = api.LiveKitAPI(failover=False)
        ctx.add_shutdown_callback(sip_api.aclose)
    control = CallControl(state, client, ctx.room, sip_api.sip if sip_api else None)

    resolver.start_phone_lookup()
    insurance = InsuranceRegistration(state, resolver, RegistrationMiddleware(client, config))
    scheduling = Scheduling(state, SchedulingHTTP(client, config))
    staff_tasks = StaffTasks(state, resolver, client, config)
    await session.start(
        agent=AbitaAgent(office, OfficeKnowledge(client, config), resolver,
                        insurance=insurance, scheduling=scheduling, staff_tasks=staff_tasks,
                        call_control=control),
        room=ctx.room,
        room_options=room_options,
    )
