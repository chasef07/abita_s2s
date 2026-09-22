"""Resolve the office and compose one voice session per LiveKit job."""

import asyncio
import os
import logging
import re
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from livekit import api, rtc
from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.call_control import CallControl
from abita_s2s.config import Config, load_config
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.middleware import PatientMiddleware
from abita_s2s.model_config import create_model
from abita_s2s.observability import log_openai_session
from abita_s2s.offices import (
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.registration_middleware import RegistrationMiddleware
from abita_s2s.reporting import CallReporter
from abita_s2s.scheduling import Scheduling
from abita_s2s.scheduling_http import SchedulingHTTP
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.state import CallContext, CallState

logger = logging.getLogger(__name__)
SIP_WAIT_SECONDS = 20
# Rescheduling can book then cancel: two 20s HTTP deadlines. Allow 10s margin.
CLEANUP_SECONDS = 50
TRANSPORT_CLOSE_SECONDS = 5
SHUTDOWN_PROCESS_SECONDS = 60


async def wait_for_sip(ctx):
    disconnected = asyncio.get_running_loop().create_future()

    def on_disconnect(*_):
        if not disconnected.done():
            disconnected.set_result(None)

    ctx.room.on("disconnected", on_disconnect)
    participant = asyncio.create_task(
        ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    )
    try:
        async with asyncio.timeout(SIP_WAIT_SECONDS):
            done, _ = await asyncio.wait(
                (participant, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done or not ctx.room.isconnected():
                raise RuntimeError("Room disconnected during SIP startup")
            return participant.result()
    finally:
        ctx.room.off("disconnected", on_disconnect)
        participant.cancel()
        disconnected.cancel()
        await asyncio.gather(participant, return_exceptions=True)


async def start_session(session, ctx, room_options, agent):
    disconnected = asyncio.get_running_loop().create_future()

    def on_disconnect(*_):
        if not disconnected.done():
            disconnected.set_result(None)

    def on_participant_disconnect(participant):
        if participant.identity == room_options.participant_identity:
            on_disconnect()

    if not ctx.is_fake_job():
        ctx.room.on("disconnected", on_disconnect)
        ctx.room.on("participant_disconnected", on_participant_disconnect)
    task = asyncio.create_task(
        session.start(agent=agent, room=ctx.room, room_options=room_options)
    )
    try:
        async with asyncio.timeout(30):
            if not ctx.is_fake_job() and (
                not ctx.room.isconnected()
                or room_options.participant_identity not in ctx.room.remote_participants
            ):
                raise RuntimeError("Caller disconnected before session startup")
            done, _ = await asyncio.wait(
                (task, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done:
                raise RuntimeError("Caller disconnected during session startup")
            await task
    finally:
        if not ctx.is_fake_job():
            ctx.room.off("disconnected", on_disconnect)
            ctx.room.off("participant_disconnected", on_participant_disconnect)
        disconnected.cancel()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def start_voice_call(ctx: JobContext, *, simulation=None) -> None:
    config = load_config()
    session_started_at = datetime.now(UTC)
    room_options = room_io.RoomOptions(
        close_on_disconnect=True,
        delete_room_on_close=True,
    )
    if simulation is not None:
        sandbox_url = os.environ.get("SANDBOX_AMD_API_URL", "").strip()
        sandbox_token = os.environ.get("SANDBOX_AMD_API_TOKEN", "").strip()
        if (
            not re.fullmatch(
                r"https://abita-middleware-sandbox-[a-z0-9.-]+\.run\.app/?", sandbox_url
            )
            or not sandbox_token
        ):
            raise ValueError(
                "Simulations require SANDBOX_AMD_API_URL and SANDBOX_AMD_API_TOKEN"
            )
        config = Config(
            openai_api_key=config.openai_api_key,
            voice=config.voice,
            knowledge_url=config.knowledge_url,
            product_secret=config.product_secret,
            middleware_url=sandbox_url,
            middleware_token=sandbox_token,
        )
        data = simulation.userdata()
        office = get_office_profile(data["office"])
        await ctx.connect()
        participant = await ctx.wait_for_participant()
        room_options.participant_identity = participant.identity
        call = CallContext(
            call_id=simulation.simulation_job_id,
            session_started_at=session_started_at,
            customer_key="abita",
            called_office_key=office.key,
            caller_phone=data.get("caller_phone"),
            room_name=ctx.room.name,
        )
    elif ctx.is_fake_job():
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
        participant = await wait_for_sip(ctx)
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
        )

    client = httpx.AsyncClient()
    insurance: InsuranceRegistration | None = None
    scheduling: Scheduling | None = None
    staff_tasks: StaffTasks | None = None

    resolver = None
    sip_api = None
    control = None
    cleanup_task = None
    drain_task = None
    state = CallState(call=call)

    async def drain():
        owners = [
            o
            for o in (resolver, scheduling, insurance, staff_tasks, control)
            if o is not None
        ]
        for owner in owners:
            owner.close_admission()
        async with asyncio.timeout(CLEANUP_SECONDS):
            results = await asyncio.gather(
                *(o.aclose() for o in owners), return_exceptions=True
            )
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise RuntimeError(
                        "An accepted mutation was cancelled during shutdown"
                    )
                if isinstance(result, BaseException):
                    raise result

    async def drain_writes():
        nonlocal drain_task
        if drain_task is None:
            drain_task = asyncio.create_task(drain())
        await asyncio.shield(drain_task)

    async def cleanup():
        try:
            if state.reporter:
                make_report = (
                    (lambda: ctx.make_session_report().to_dict())
                    if state.reporter.started
                    else None
                )
                await state.reporter.finish(make_report)
            else:
                await drain_writes()
        finally:
            # A failed owner must not skip either transport.
            async with asyncio.timeout(TRANSPORT_CLOSE_SECONDS):
                results = await asyncio.gather(
                    client.aclose(),
                    *([sip_api.aclose()] if sip_api else []),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

    async def close_client():
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(cleanup())
        await asyncio.shield(cleanup_task)

    ctx.add_shutdown_callback(close_client)
    if config.interaction_url and not ctx.is_fake_job():
        if call.caller_phone:
            state.reporter = CallReporter(call, client, config, drain_writes)
        else:
            logger.error("Product call reporting unavailable: caller phone missing")
    try:
        session = AgentSession[CallState](
            userdata=state,
            llm=create_model(config),
            vad=None,
            turn_handling={"turn_detection": "realtime_llm"},
        )
        session.on(
            "error",
            lambda event: logger.error(
                "session_error cause=%s recoverable=%s",
                type(event.error).__name__,
                getattr(event.error, "recoverable", False),
            ),
        )
        session.on(
            "close", lambda event: logger.info("session_closed reason=%s", event.reason)
        )
        resolver = PatientResolver(state, PatientMiddleware(client, config))
        if not ctx.is_fake_job() and simulation is None:
            sip_api = api.LiveKitAPI(failover=False)
        control = CallControl(
            state,
            client,
            ctx.room,
            sip_api.sip if sip_api else None,
            handoff=config.handoff,
        )

        resolver.start_phone_lookup()
        insurance = InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, config)
        )
        scheduling = Scheduling(state, SchedulingHTTP(client, config))
        staff_tasks = StaffTasks(state, resolver, client, config)
        await start_session(
            session,
            ctx,
            room_options,
            AbitaAgent(
                office,
                OfficeKnowledge(client, config),
                resolver,
                insurance=insurance,
                scheduling=scheduling,
                staff_tasks=staff_tasks,
                call_control=control,
            ),
        )
        if state.reporter:
            state.reporter.started = True
        log_openai_session(session.current_agent.duplex_session, call.call_id)
    except BaseException as error:
        logger.error("session_start_failed cause=%s", type(error).__name__)
        try:
            await close_client()
        except Exception:
            logger.error("Startup cleanup failed")
        raise
    logger.info("session_started")


async def finish_voice_call(ctx: JobContext) -> None:
    try:
        state = ctx.primary_session.userdata
    except RuntimeError:
        return  # Startup cleanup owns the failed closeout before session registration.
    if state.reporter:
        await state.reporter.finish(lambda: ctx.make_session_report().to_dict())
