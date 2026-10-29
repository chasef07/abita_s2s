"""Resolve the office and compose one voice session per LiveKit job."""

import asyncio
import os
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from livekit import api, rtc
from livekit.agents import AgentSession, JobContext, room_io

from abita_s2s.agent import AbitaAgent
from abita_s2s.budgets import DRAIN, TRANSPORT_CLOSE
from abita_s2s.call_control import CallControl
from abita_s2s.config import load_config
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.insurance_list import (
    OfficePlans,
    read_insurance_list,
    render_insurance_list,
)
from abita_s2s.knowledge import read_office_knowledge, render_office_knowledge
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.product import KNOWLEDGE_ENTRIES, Product
from abita_s2s.model_config import create_model
from abita_s2s.offices import (
    get_office_profile,
    get_office_profile_by_phone,
)
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.runtime.reporting import CallReporter
from abita_s2s.runtime.silence import check_in_on_silence
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.integrations.scheduling_http import SchedulingHTTP
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.state import CallContext, CallState
from abita_s2s.tools.registry import Owners

logger = logging.getLogger(__name__)
SIP_WAIT_SECONDS = 20


def _disconnect_signal():
    """Return a future and an idempotent event handler that resolves it."""
    disconnected = asyncio.get_running_loop().create_future()

    def on_disconnect(*_):
        if not disconnected.done():
            disconnected.set_result(None)

    return disconnected, on_disconnect


async def _gather_raising(*steps, cancelled: str | None = None):
    """Await every step to completion, then re-raise the first failure."""
    results = await asyncio.gather(*steps, return_exceptions=True)
    for result in results:
        if cancelled and isinstance(result, asyncio.CancelledError):
            raise RuntimeError(cancelled)
        if isinstance(result, BaseException):
            raise result


async def office_knowledge_text(
    product: Product, office_key: str, scenario: object | None
) -> str:
    """The office's knowledge, or a simulation scenario's synthetic entries."""
    if scenario is None:
        return await read_office_knowledge(product, office_key)
    return render_office_knowledge(KNOWLEDGE_ENTRIES.validate_python(scenario))


async def insurance_list_text(
    middleware: RegistrationMiddleware, office_key: str, scenario: object | None
) -> str:
    """The office's insurance list, or a simulation scenario's synthetic plans."""
    if scenario is None:
        return await read_insurance_list(middleware, office_key)
    return render_insurance_list(OfficePlans.model_validate(scenario))


async def wait_for_sip(ctx):
    disconnected, on_disconnect = _disconnect_signal()
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
    disconnected, on_disconnect = _disconnect_signal()

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
    scenario_knowledge = scenario_plans = None
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
        config = replace(
            config,
            middleware_url=sandbox_url,
            middleware_token=sandbox_token,
            staff_tasks_url=None,
            interaction_url=None,
            handoff=None,
        )
        data = simulation.userdata()
        office = get_office_profile(data["office"])
        scenario_knowledge = data.get("office_knowledge")
        scenario_plans = data.get("insurance_plans")
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
    owners: Owners | None = None
    sip_api = None
    cleanup_task = None
    drain_task = None
    state = CallState(call=call)

    async def drain():
        writers = owners.writers() if owners else []
        for writer in writers:
            writer.close_admission()
        async with asyncio.timeout(DRAIN.seconds):
            await _gather_raising(
                *(writer.aclose() for writer in writers),
                cancelled="An accepted mutation was cancelled during shutdown",
            )

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
            async with asyncio.timeout(TRANSPORT_CLOSE.seconds):
                await _gather_raising(
                    client.aclose(), *([sip_api.aclose()] if sip_api else [])
                )

    async def close_client():
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(cleanup())
        await asyncio.shield(cleanup_task)

    ctx.add_shutdown_callback(close_client)
    product = Product(client, config)
    registration = RegistrationMiddleware(client, config)
    office_knowledge, insurance_list = await asyncio.gather(
        office_knowledge_text(product, office.key, scenario_knowledge),
        insurance_list_text(registration, office.key, scenario_plans),
    )
    if config.interaction_url and not ctx.is_fake_job():
        if call.caller_phone:
            state.reporter = CallReporter(
                call, client, config, drain_writes, office_knowledge
            )
        else:
            logger.error("Product call reporting unavailable: caller phone missing")
    try:
        model = create_model(config, office_knowledge, insurance_list)
        session = AgentSession[CallState](
            userdata=state,
            llm=model,
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
            product,
            ctx.room,
            sip_api.sip if sip_api else None,
            sandbox=config.deployment is not None,
            simulation=simulation is not None,
            backend=model.backend,
        )
        owners = Owners(
            office,
            resolver=resolver,
            scheduling=Scheduling(state, SchedulingHTTP(client, config)),
            insurance=InsuranceRegistration(state, resolver, registration),
            staff_tasks=StaffTasks(state, resolver, product),
            call_control=control,
        )
        check_in_on_silence(
            session,
            model.backend,
            control,
            lambda: model.watchdog.hand_off_held(),
        )
        resolver.start_phone_lookup()
        await start_session(session, ctx, room_options, AbitaAgent(owners))
        if state.reporter:
            state.reporter.started = True
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
        return
    if state.reporter:
        await state.reporter.finish(lambda: ctx.make_session_report().to_dict())
