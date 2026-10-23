"""Replay fixed caller scripts against the real speaker, thinker, and tools, offline except for the model API.

From the repository root, with OPENAI_API_KEY set and nothing else:

    uv run --no-sync python -m evals.replay

The caller speaks synthesized audio, because GPT-Live answers only what it hears; typed
input reaches it as a note, not as the caller. Owners run on ``world.World``, so no request
leaves this process except to api.openai.com. No LiveKit server, room, or worker is used.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from livekit import rtc
from livekit.agents import AgentSession, llm
from livekit.agents.metrics import LLMModelUsage
from livekit.agents.testing import fake_job_context
from livekit.agents.voice.io import AudioInput
from livekit.plugins.openai.realtime import GPTLiveSession

from abita_s2s.agent import AbitaAgent
from abita_s2s.call_control import CallControl
from abita_s2s.identity import PatientResolver
from abita_s2s.insurance import InsuranceRegistration
from abita_s2s.integrations.patient_middleware import PatientMiddleware
from abita_s2s.integrations.product import Product
from abita_s2s.integrations.registration_middleware import RegistrationMiddleware
from abita_s2s.integrations.scheduling_http import SchedulingHTTP
from abita_s2s.knowledge import OfficeKnowledge
from abita_s2s.model_config import SPEAKER_MODEL, CallModel, create_model
from abita_s2s.offices import get_office_profile
from abita_s2s.runtime.silence import check_in_on_silence
from abita_s2s.scheduling.appointments import Scheduling
from abita_s2s.staff_tasks import StaffTasks
from abita_s2s.state import CallContext, CallState
from abita_s2s.tools.registry import Owners
from evals.replay.checks import Agent, Call, Caller, Event, Output
from evals.replay.scenarios import SCENARIOS, Scenario
from evals.replay.world import CALLER_PHONE, World, config

RATE = 24000
FRAME = RATE // 50
BREATH_SECONDS = 0.6
QUIET_SECONDS = 3.0
REACTION_SECONDS = 10.0
TURN_SECONDS = 45.0
SPEECH_URL = "https://api.openai.com/v1/audio/speech"
SPEECH_MODEL = "gpt-4o-mini-tts"
SPEECH_VOICE = "alloy"


class Budget:
    """Model calls this run has made: caller lines synthesized, voice sessions, and thinker responses."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls = 0
        self.exceeded = asyncio.Event()

    def spend(self) -> None:
        self.calls += 1
        if self.calls > self.limit:
            self.exceeded.set()


class CallerAudio(AudioInput):
    """The caller's line: queued speech, then silence, at real-time pace, since GPT-Live speaks only while it hears audio."""

    def __init__(self) -> None:
        super().__init__(label="replay-caller")
        self._queued = bytearray()
        self._due: float | None = None

    def say(self, pcm: bytes) -> float:
        """Queue 24 kHz mono PCM; returns the seconds until everything queued has played."""
        self._queued.extend(pcm)
        return len(self._queued) / (2 * RATE)

    async def __anext__(self) -> rtc.AudioFrame:
        now = time.monotonic()
        self._due = (self._due or now) + FRAME / RATE
        await asyncio.sleep(max(0.0, self._due - now))
        chunk = bytes(self._queued[: 2 * FRAME]).ljust(2 * FRAME, b"\0")
        del self._queued[: 2 * FRAME]
        return rtc.AudioFrame(chunk, RATE, 1, FRAME)


class Activity:
    """When GPT-Live last spoke, heard the caller, or ran the thinker; it also counts thinker responses.

    LiveKit's agent state is no help here: GPT-Live streams audio without pause, so the
    state stays ``speaking`` after the words end.
    """

    AGENT = frozenset(
        {
            "session.output_transcript.delta",
            "session.delegation.created",
            "response.event",
        }
    )

    def __init__(self, budget: Budget) -> None:
        self.at = time.monotonic()
        self.reacted = 0.0
        self._budget = budget

    def follow(self, live: GPTLiveSession) -> None:
        live.on("openai_server_event_received", self.record)

    def record(self, event: dict) -> None:
        kind = event.get("type")
        if kind in self.AGENT:
            self.at = self.reacted = time.monotonic()
        elif kind == "session.input_transcript.delta":
            self.at = time.monotonic()
        if event.get("event", {}).get("type") == "response.created":
            self._budget.spend()


@dataclass
class Result:
    name: str
    mirrors: str
    failures: list[str]
    transcript: list[dict]
    speaker_seconds: float = 0.0
    thinker: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


async def settle(
    model: CallModel, activity: Activity, closed: asyncio.Event, since: float
) -> bool:
    """Wait for the agent's turn to end; False when it runs past TURN_SECONDS.

    The turn ends when the call ends, when the agent has not reacted since ``since``
    within REACTION_SECONDS, or when it reacted and then went QUIET_SECONDS with no
    speech and no backend work.
    """
    start = time.monotonic()
    activity.at = max(activity.at, start)
    while (now := time.monotonic()) - start < TURN_SECONDS:
        if closed.is_set():
            return True
        if model.backend.busy:
            activity.at = now
        elif activity.reacted < since:
            if now - start >= REACTION_SECONDS:
                return True
        elif now - activity.at >= QUIET_SECONDS:
            return True
        await asyncio.sleep(0.1)
    return False


def transcript(
    history: llm.ChatContext, heard: list[tuple[float, Caller]]
) -> list[Event]:
    """The call in time order: the caller's lines as played, and the agent's speech, tool calls, and results."""
    timed: list[tuple[float, Event]] = list(heard)
    for item in history.items:
        if isinstance(item, llm.ChatMessage) and item.role == "assistant":
            if text := item.text_content:
                timed.append((item.created_at, Agent(text)))
        elif isinstance(item, llm.FunctionCall):
            arguments = json.loads(item.arguments or "{}")
            timed.append((item.created_at, Call(item.name, arguments, item.call_id)))
        elif isinstance(item, llm.FunctionCallOutput):
            timed.append(
                (item.created_at, Output(item.name, item.output, item.call_id))
            )
    return [event for _, event in sorted(timed, key=lambda pair: pair[0])]


async def drain(owners: Owners) -> list[str]:
    writers = owners.writers()
    for writer in writers:
        writer.close_admission()
    results = await asyncio.gather(
        *(writer.aclose() for writer in writers), return_exceptions=True
    )
    return [
        f"shutdown failed: {type(result).__name__}"
        for result in results
        if isinstance(result, BaseException)
    ]


async def play(
    scenario: Scenario, voice: dict[str, bytes], budget: Budget, api_key: str
) -> Result:
    """Run one scripted call against fixture owners and judge it."""
    began = time.monotonic()
    world = World()
    settings = config(api_key)
    office = get_office_profile(scenario.office)
    state = CallState(
        call=CallContext(
            call_id=f"replay-{scenario.name}",
            session_started_at=datetime.now(UTC),
            customer_key="abita",
            called_office_key=office.key,
            caller_phone=CALLER_PHONE,
        )
    )
    client = world.client()
    model = create_model(settings)
    product = Product(client, settings)
    control = CallControl(state, product, simulation=True, backend=model.backend)
    resolver = PatientResolver(state, PatientMiddleware(client, settings))
    owners = Owners(
        office,
        OfficeKnowledge(product),
        resolver=resolver,
        scheduling=Scheduling(state, SchedulingHTTP(client, settings)),
        insurance=InsuranceRegistration(
            state, resolver, RegistrationMiddleware(client, settings)
        ),
        staff_tasks=StaffTasks(state, resolver, product),
        call_control=control,
    )
    session = AgentSession[CallState](
        userdata=state,
        llm=model,
        vad=None,
        turn_handling={"turn_detection": "realtime_llm"},
    )
    caller = CallerAudio()
    session.input.audio = caller
    activity = Activity(budget)
    closed = asyncio.Event()
    session.on("close", lambda _: closed.set())
    check_in_on_silence(
        session, model.backend, control, lambda: model.watchdog.hand_off_held()
    )
    agent = AbitaAgent(owners)
    heard: list[tuple[float, Caller]] = []
    failures: list[str] = []
    with fake_job_context(job_id=f"replay-{scenario.name}"):
        budget.spend()
        resolver.start_phone_lookup()
        try:
            await session.start(agent, record=False)
            live = agent.duplex_session
            if not isinstance(live, GPTLiveSession):
                raise TypeError("the replay drives GPT-Live only")
            activity.follow(live)
            if not await settle(model, activity, closed, time.monotonic()):
                failures.append(
                    f"the greeting did not settle within {TURN_SECONDS:.0f} s"
                )
            for turn, breaths in enumerate(scenario.turns):
                began_turn = time.monotonic()
                for n, line in enumerate(breaths):
                    if closed.is_set():
                        break
                    if n:
                        await asyncio.sleep(BREATH_SECONDS)
                    heard.append((time.time(), Caller(turn, line)))
                    await asyncio.sleep(caller.say(voice[line]))
                if closed.is_set():
                    break
                if not await settle(model, activity, closed, began_turn):
                    failures.append(
                        f"caller turn {turn} did not settle within {TURN_SECONDS:.0f} s"
                    )
        finally:
            failures += await drain(owners)
            await session.aclose()
            await client.aclose()
            await model.aclose()
    events = transcript(session.history, heard)
    failures += [f"no fixture route for {request}" for request in world.unrouted]
    failures += [failure for check in scenario.checks if (failure := check(events))]
    usage = [u for u in session.usage.model_usage if isinstance(u, LLMModelUsage)]
    thinker = [u for u in usage if u.model != SPEAKER_MODEL]
    return Result(
        scenario.name,
        scenario.mirrors,
        failures,
        [{"kind": type(e).__name__.lower(), **asdict(e)} for e in events],
        sum(u.session_duration for u in usage if u.model == SPEAKER_MODEL),
        {
            "input": sum(u.input_tokens for u in thinker),
            "cached": sum(u.input_cached_tokens for u in thinker),
            "output": sum(u.output_tokens for u in thinker),
            "reasoning": sum(u.output_reasoning_tokens for u in thinker),
        },
        time.monotonic() - began,
    )


async def synthesize(lines: set[str], api_key: str, budget: Budget) -> dict[str, bytes]:
    """Each distinct caller line as 24 kHz mono PCM, spoken by the model API's text-to-speech."""
    gate = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=60) as http:

        async def speak(line: str) -> tuple[str, bytes]:
            async with gate:
                budget.spend()
                reply = await http.post(
                    SPEECH_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": SPEECH_MODEL,
                        "voice": SPEECH_VOICE,
                        "input": line,
                        "response_format": "pcm",
                    },
                )
                reply.raise_for_status()
                return line, reply.content

        return dict(await asyncio.gather(*(speak(line) for line in sorted(lines))))


async def replay(
    scenarios: list[Scenario], api_key: str, budget: Budget, concurrency: int
) -> list[Result] | None:
    """Every scenario's result, or None when the run reached the model-call limit and stopped."""
    voice = await synthesize(
        {line for s in scenarios for turn in s.turns for line in turn}, api_key, budget
    )
    gate = asyncio.Semaphore(concurrency)

    async def one(scenario: Scenario) -> Result:
        async with gate:
            return await play(scenario, voice, budget, api_key)

    calls = asyncio.gather(*(one(scenario) for scenario in scenarios))
    stop = asyncio.ensure_future(budget.exceeded.wait())
    await asyncio.wait({calls, stop}, return_when=asyncio.FIRST_COMPLETED)
    if budget.exceeded.is_set():
        calls.cancel()
        await asyncio.gather(calls, return_exceptions=True)
        return None
    stop.cancel()
    return calls.result()


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def report(results: list[Result], budget: Budget, seconds: float) -> str:
    """The verdict table and the run's model spend, as Markdown."""
    rows = [
        "| Scenario | Mirrors | Verdict | Why |",
        "| --- | --- | --- | --- |",
        *(
            f"| {r.name} | {r.mirrors} | {'FAIL' if r.failures else 'pass'} | "
            f"{cell('; '.join(r.failures))} |"
            for r in results
        ),
    ]
    tokens = {
        key: sum(r.thinker.get(key, 0) for r in results)
        for key in ("input", "cached", "output", "reasoning")
    }
    return "\n".join(
        [
            *rows,
            "",
            f"Model calls: {budget.calls} of {budget.limit} allowed. "
            f"Speaker: {sum(r.speaker_seconds for r in results):.0f} s of GPT-Live audio. "
            f"Thinker: {tokens['input']} input tokens ({tokens['cached']} cached), "
            f"{tokens['output']} output tokens ({tokens['reasoning']} reasoning). "
            f"Wall time: {seconds:.0f} s.",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.replay", description=__doc__)
    parser.add_argument("--only", nargs="+", choices=[s.name for s in SCENARIOS])
    parser.add_argument("--concurrency", type=int, default=len(SCENARIOS))
    parser.add_argument("--max-calls", type=int, default=200)
    parser.add_argument(
        "--out", type=Path, help="write every transcript and verdict as JSON"
    )
    parser.add_argument(
        "--summary", type=Path, help="append the Markdown table to this file"
    )
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set; the replay needs only the model API key.")
        return 2
    scenarios = [s for s in SCENARIOS if not args.only or s.name in args.only]
    budget = Budget(args.max_calls)
    began = time.monotonic()
    results = asyncio.run(replay(scenarios, api_key, budget, args.concurrency))
    if results is None:
        print(f"Stopped: the run passed {budget.limit} model calls.")
        return 2
    for result in results:
        print(f"--- {result.name}: {'FAIL' if result.failures else 'pass'}")
        for event in result.transcript:
            print(f"    {json.dumps(event, ensure_ascii=False)}")
    table = report(results, budget, time.monotonic() - began)
    print(table)
    if args.summary:
        with args.summary.open("a") as summary:
            summary.write("## Offline replay\n\n" + table + "\n")
    if args.out:
        args.out.write_text(
            json.dumps(
                {"calls": budget.calls, "results": [asdict(r) for r in results]},
                indent=2,
                ensure_ascii=False,
            )
        )
    return 1 if any(r.failures for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
